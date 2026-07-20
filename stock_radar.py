"""Application service for scans, live trigger monitoring and safe staging."""
from __future__ import annotations

import os
import threading
import time as time_module
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from core.events import trading_clock, trading_today
from core.ib_client import with_ib
from core.reprice import reprice_cards
from core.stock_data import quotes_tws, quotes_yf, scan_stocks
from execution.candidates import validate_for_stage, within_execution_window
from execution.stage import stage_suggestion
from execution.stock_orders import approve_lots, make_stageable, snap_vertical_live
from portfolio.accounts import list_accounts
from selection.stock_radar import model_card, trigger_state
from store.campaigns import campaign_store
from store.radar import radar_store

NY = ZoneInfo("America/New_York")


def run_scan(cadence: str = "daily", source: str = "yf", limit: int | None = None) -> dict:
    store = radar_store()
    payload = scan_stocks(cadence=cadence, source=source, limit=limit,
                          previous_symbols=store.previous_symbols(cadence))
    return store.save(payload)


def latest_watchlist(cadence: str = "daily") -> dict:
    out = radar_store().latest(cadence)
    if not out:
        raise ValueError(f"no saved {cadence} radar; run the after-close scan first")
    return out


def due_cadences(now: datetime | None = None, source: str | None = None) -> list[str]:
    """Return watchlists due after the US close, using New York time only."""
    now = (now or datetime.now(NY)).astimezone(NY)
    if now.weekday() >= 5 or now.time().replace(tzinfo=None) < time(16, 10):
        return []
    session = now.date().isoformat()
    due = []
    daily = radar_store().latest("daily")
    if (not daily or daily.get("session") != session or
            (source is not None and daily.get("source") != source)):
        due.append("daily")
    if now.weekday() == 4:
        weekly = radar_store().latest("weekly")
        if (not weekly or weekly.get("session") != session or
                (source is not None and weekly.get("source") != source)):
            due.append("weekly")
    return due


def run_due_scans(source: str = "yf", now: datetime | None = None) -> list[dict]:
    return [run_scan(cadence, source) for cadence in due_cadences(now, source)]


def _account_profile(ib, account: str | None, supplied: float | None) -> dict:
    accounts = list_accounts(ib)
    if not account and len(accounts) == 1:
        account = accounts[0]["account"]
    if not account:
        raise ValueError("select the IBKR account before enabling live staging")
    profile = next((x for x in accounts if x["account"] == account), None)
    if not profile:
        raise ValueError(f"IBKR account {account} is not available on this TWS session")
    actual_nlv = profile.get("nlv")
    if actual_nlv is None or float(actual_nlv) <= 0:
        raise ValueError(f"IBKR account {account} has no usable NetLiquidation value")
    resolved_nlv = float(actual_nlv)
    if supplied is not None and float(supplied) > 0:
        resolved_nlv = min(resolved_nlv, float(supplied))
    return {**profile, "account": account, "nlv": resolved_nlv,
            "actual_nlv": float(actual_nlv)}


def _account_symbols(ib, account: str | None) -> set[str]:
    if not account:
        return set()
    symbols = {p.contract.symbol for p in ib.positions()
               if p.account == account and float(p.position) != 0}
    for trade in ib.openTrades():
        if getattr(trade.order, "account", None) in (None, "", account):
            symbol = getattr(trade.contract, "symbol", None)
            if symbol:
                symbols.add(symbol)
    return symbols


def _persist_card(idea: dict, card: dict, account: str | None, mode: str,
                  spot: float, nlv: float, trigger: dict) -> str:
    clock = trading_clock()
    context = {"session": clock["ny_date"], "fresh": mode in ("live", "mock"),
               "spot": spot, "as_of_time": clock["ny_time"], "nlv": nlv,
               "idea": idea, "trigger": trigger,
               "test_session_id": f"STOCK-{clock['ny_date']}-{idea['symbol']}"}
    return campaign_store().save_candidate(idea["symbol"], account, mode,
                                           "stock-radar-v1", context, card,
                                           ttl_seconds=300 if mode == "live" else 86400)


def _monitor_rows(ideas: list[dict], quotes: dict[str, dict], *, mode: str,
                  in_window: bool, ib=None, account: str | None = None,
                  nlv: float = 100_000, available_funds: float | None = None,
                  account_symbols: set[str] | None = None) -> list[dict]:
    rows = []
    today = trading_today()
    for idea in ideas:
        overlap = idea["symbol"] in (account_symbols or set())
        visible_idea = {**idea, "risk_flags": list(idea.get("risk_flags", [])),
                        "position_overlap": overlap}
        if overlap:
            visible_idea["risk_flags"].append(
                "Existing position or working order in this account: portfolio review required.")
        q = quotes.get(idea["symbol"])
        if not q:
            rows.append({**visible_idea, "monitor": {"state": "NO_DATA", "flash": False,
                                                      "reason": "No current quote."},
                         "trade_card": model_card(idea, today)})
            continue
        state = trigger_state(idea, float(q["price"]), fresh=bool(q.get("fresh")),
                              in_last_hour=in_window)
        card = model_card(idea, today, float(q["price"]))
        error = None
        if state["state"] == "TRIGGERED" and mode == "live" and ib is not None:
            try:
                card = snap_vertical_live(ib, idea, card, float(q["price"]), today)
                card = make_stageable(card, idea, nlv, state, available_funds, overlap)
                if card["permitted"]:
                    card["candidate_id"] = _persist_card(
                        idea, card, account, mode, float(q["price"]), nlv, state)
            except Exception as exc:  # noqa: BLE001 — visible per ticker, monitor continues
                error = str(exc)
                card.update(permitted=False, tws_stage_allowed=False, status="WAIT",
                            blocks=[error])
        elif state["state"] == "TRIGGERED" and mode == "mock":
            card.update(permitted=True, tws_stage_allowed=True, status="ENTER",
                        governor={"approved_lots": 1, "risk_approved": True,
                                  "risk_per_lot": card["cash_required"],
                                  "risk_budget": nlv * .005})
            card["candidate_id"] = _persist_card(
                idea, card, account or "MOCK-A", mode, float(q["price"]), nlv, state)
        rows.append({**visible_idea, "quote": q, "monitor": state, "trade_card": card,
                     "construction_error": error})
    return rows


def monitor(cadence: str = "daily", mode: str = "auto", account: str | None = None,
            nlv: float | None = None) -> dict:
    watch = latest_watchlist(cadence)
    if watch.get("source") == "mock" and mode != "mock":
        raise ValueError("practice watchlists can only use practice monitoring; run a yfinance scan first")
    try:
        session_age = (trading_today() - date.fromisoformat(watch["session"])).days
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("saved watchlist has an invalid market session; rescan") from exc
    max_age = 8 if cadence == "weekly" else 4
    if session_age < 0 or session_age > max_age:
        raise ValueError(f"saved {cadence} watchlist is stale ({session_age} calendar days); rescan")
    ideas = watch["candidates"]
    symbols = [x["symbol"] for x in ideas]
    errors = []
    actual_mode = mode
    if mode in ("live", "auto"):
        try:
            def live_job(ib):
                profile = _account_profile(ib, account, nlv)
                q = quotes_tws(ib, symbols)
                account_symbols = _account_symbols(ib, profile["account"])
                return profile, _monitor_rows(
                    ideas, q, mode="live", in_window=within_execution_window(),
                    ib=ib, account=profile["account"], nlv=profile["nlv"],
                    available_funds=profile.get("available_funds"),
                    account_symbols=account_symbols)
            profile, rows = with_ib(live_job)
            account, nlv = profile["account"], profile["nlv"]
            available_funds, cash = profile.get("available_funds"), profile.get("cash")
            actual_mode = "live"
        except Exception as exc:
            errors.append(f"live: {exc}")
            if mode == "live":
                raise RuntimeError(errors[-1]) from exc
    if actual_mode not in ("live",) or (mode == "auto" and errors):
        if mode == "mock":
            quotes = {}
            for i, idea in enumerate(ideas):
                trigger = float(idea["trigger"]["price"])
                px = trigger * (1.001 if idea["direction"] == "BULL" else .999) if i == 0 else idea["price"]
                quotes[idea["symbol"]] = {"price": px, "fresh": True,
                                           "source": "mock live", "captured_at": datetime.now(NY).isoformat()}
            rows = _monitor_rows(ideas, quotes, mode="mock", in_window=True,
                                 account=account or "MOCK-A", nlv=float(nlv or 100_000),
                                 available_funds=float(nlv or 100_000))
            actual_mode = "mock"
            available_funds, cash = float(nlv or 100_000), float(nlv or 100_000)
        else:
            quotes = quotes_yf(symbols)
            rows = _monitor_rows(ideas, quotes, mode="yf", in_window=False,
                                 account=account, nlv=float(nlv or 100_000))
            actual_mode = "yf"
            available_funds, cash = None, None
    clock = trading_clock()
    return {"policy_id": "stock-radar-v1", "mode": actual_mode,
            "cadence": cadence, "snapshot_id": watch["snapshot_id"],
            "session": watch["session"], "account": account, "nlv": nlv,
            "available_funds": available_funds, "cash": cash,
            "clock": clock, "standard_window": within_execution_window(),
            "upcoming_tier1": watch.get("upcoming_tier1", []),
            "fallback_chain": errors, "candidates": rows,
            "triggered": sum(x["monitor"]["state"] == "TRIGGERED" for x in rows),
            "armed": sum(x["monitor"]["state"] == "ARMED" for x in rows),
            "safety": "Alerts never place orders. Stage buttons create transmit=False TWS combos only."}


def stage(candidate_id: str, quantity: int = 1) -> dict:
    checked = validate_for_stage(candidate_id, quantity)
    cand, stored, qty = checked["candidate"], checked["card"], checked["quantity"]
    if cand["policy_id"] != "stock-radar-v1":
        raise ValueError("candidate is not a Stock Opportunity Radar order")
    if cand["mode"] == "mock":
        result = {"orderId": -1, "status": "MockStaged", "transmit": False,
                  "candidate_id": candidate_id, "qty": qty, "legs": stored["legs_raw"],
                  "warnings": checked["warnings"]}
        campaign_store().record_order(candidate_id, qty, result)
        return result
    if cand.get("context", {}).get("idea", {}).get("data_source") != "yf":
        raise ValueError("only a yfinance after-close watchlist can stage a live stock order")

    def live_job(ib):
        ctx, idea = cand["context"], cand["context"]["idea"]
        profile = _account_profile(ib, cand["account"], None)
        if profile.get("available_funds") is None:
            raise ValueError("IBKR AvailableFunds is unavailable; order not staged")
        if cand["symbol"] in _account_symbols(ib, cand["account"]):
            raise ValueError("existing position or working order requires portfolio review")
        quote = quotes_tws(ib, [cand["symbol"]]).get(cand["symbol"])
        if not quote:
            raise RuntimeError("fresh underlying quote unavailable")
        trig = trigger_state(idea, quote["price"], fresh=True,
                             in_last_hour=within_execution_window())
        if trig["state"] != "TRIGGERED":
            raise ValueError("trigger no longer active inside 15:00–15:40 ET; order not staged")
        live = {**stored, "rationale": list(stored.get("rationale", [])),
                "legs_raw": [dict(x) for x in stored["legs_raw"]]}
        reprice_cards(ib, cand["symbol"], quote["price"], trading_today(), [live])
        if live.get("mid_src") != "live" or live.get("liquidity", {}).get("flagged"):
            raise RuntimeError("exact option legs failed the refreshed NBBO liquidity check")
        width = abs(float(live["legs_raw"][1]["strike"]) - float(live["legs_raw"][0]["strike"]))
        if live["net_mid"] <= 0 or live["net_mid"] > width * .45:
            raise ValueError("refreshed debit no longer satisfies the 45%-of-width entry rule")
        sizing_nlv = min(float(ctx.get("nlv") or profile["nlv"]), profile["nlv"])
        gov = approve_lots(live, sizing_nlv,
                           available_funds=profile.get("available_funds"))
        if qty > gov["approved_lots"]:
            raise ValueError(f"fresh risk budget approves {gov['approved_lots']} lots, requested {qty}")
        return stage_suggestion(ib, cand["symbol"], live["legs_raw"], live["net_mid"],
                                qty, transmit=False, account=cand["account"])

    result = with_ib(live_job)
    result = {**result, "candidate_id": candidate_id, "transmit": False,
              "warnings": checked["warnings"]}
    campaign_store().record_order(candidate_id, qty, result)
    return result


class RadarScheduler:
    """NY-time after-close scheduler used while ``webapp.py`` is running."""
    def __init__(self, source: str = "yf"):
        self.source = source
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_attempt: dict[tuple[str, str], float] = {}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, daemon=True, name="stock-radar-scheduler")
        self.thread.start()

    def _loop(self):
        while not self.stop_event.wait(30):
            now = datetime.now(NY)
            session = now.date().isoformat()
            for cadence in due_cadences(now, self.source):
                key = (session, cadence)
                last = self.last_attempt.get(key, 0.0)
                if time_module.monotonic() - last < 30 * 60:
                    continue
                self.last_attempt[key] = time_module.monotonic()
                try:
                    run_scan(cadence, self.source)
                except Exception as exc:  # scanner can be rerun manually; app remains healthy
                    print(f"Stock radar scheduled {cadence} scan failed: {exc}")


def scheduler_enabled() -> bool:
    return os.getenv("FVS_RADAR_AUTOSCAN", "1").lower() not in ("0", "false", "no")
