"""Application service for scans, live trigger monitoring and safe staging."""
from __future__ import annotations

import os
import threading
import time as time_module
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from core.events import macro_risk_gate, trading_clock, trading_today
from core.ib_client import with_ib
from core.reprice import reprice_cards
from core.stock_data import quotes_tws, quotes_yf, scan_stocks
from execution.candidates import validate_for_stage, within_execution_window
from execution.stage import stage_suggestion
from execution.stock_orders import make_stageable, snap_vertical_live
from portfolio.accounts import list_accounts
from selection.stock_radar import (ACTIVE_LIMIT, POLICY_ID, challenger_rank,
                                   model_card, trigger_state)
from store.campaigns import campaign_store
from store.radar import radar_store

NY = ZoneInfo("America/New_York")


def run_scan(cadence: str = "daily", source: str = "yf", limit: int | None = None) -> dict:
    store = radar_store()
    payload = scan_stocks(cadence=cadence, source=source, limit=limit,
                          previous_symbols=store.previous_symbols(cadence))
    saved = store.save(payload)
    if source == "yf":
        try:
            from core.stock_data import histories_yf
            saved["outcome_update"] = store.update_outcomes(
                histories_yf(store.shadow_symbols()))
        except Exception as exc:  # evidence refresh must not invalidate a scan
            saved["outcome_update"] = {"error": str(exc)}
    return saved


def latest_watchlist(cadence: str = "daily") -> dict:
    out = radar_store().latest(cadence)
    if not out:
        raise ValueError(f"no saved {cadence} radar; run the after-close scan first")
    return out


def _previous_weekday(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def _expected_baseline_session(now: datetime | None = None) -> date:
    now = (now or datetime.now(NY)).astimezone(NY)
    if now.weekday() >= 5:
        return _previous_weekday(now.date())
    if now.time().replace(tzinfo=None) >= time(16, 10):
        return now.date()
    return _previous_weekday(now.date())


def ensure_prepared(cadence: str, mode: str, now: datetime | None = None) -> tuple[dict, bool]:
    """Catch up a missing/stale baseline when the operator starts late."""
    watch = radar_store().latest(cadence)
    if watch and watch.get("source") == "mock":
        if mode != "mock":
            raise ValueError("practice watchlists can only use practice monitoring; run a yfinance scan first")
        return watch, False
    if mode == "mock":
        if not watch:
            raise ValueError("no practice watchlist; run the Practice data scan first")
        return watch, False
    expected = _expected_baseline_session(now)
    stale = not watch or watch.get("policy_id") != POLICY_ID
    if watch:
        try:
            stale = date.fromisoformat(watch["session"]) < expected
        except (KeyError, TypeError, ValueError):
            stale = True
    if stale:
        return run_scan(cadence, "yf"), True
    return watch, False


def _live_selection(watch: dict, quotes: dict[str, dict], cadence: str,
                    now: datetime | None = None) -> tuple[list[dict], dict]:
    now = (now or datetime.now(NY)).astimezone(NY)
    session = now.date().isoformat()
    store = radar_store()
    existing = store.live_session(session, cadence)
    if existing and existing.get("frozen_at"):
        return existing["candidates"], existing
    research = watch.get("research_pool") or watch["candidates"]
    comparison = challenger_rank(research, quotes)
    baseline = [{**x, "list_role": "ACTIVE", "cohort": "v1_static",
                 "shadow_only": False} for x in comparison["baseline"]]
    refresh_due = now.weekday() < 5 and now.time().replace(tzinfo=None) >= time(14, 45)
    shadow = []
    if refresh_due:
        shadow = [{**x, "list_role": "CHALLENGER", "cohort": "challenger",
                   "shadow_only": True} for x in comparison["shadow_promotions"]]
        store.record_shadow_candidates(watch["snapshot_id"], watch["session"],
                                       "challenger", shadow)
    shadow_symbols = {x["symbol"] for x in shadow}
    reserves = [{**x, "list_role": "RESERVE", "cohort": "v1_reserve",
                 "shadow_only": False}
                for x in watch["candidates"][ACTIVE_LIMIT:]
                if x["symbol"] not in shadow_symbols]
    reserves = reserves[:max(0, 5 - len(shadow))]
    payload = {"candidates": baseline + reserves + shadow,
               "phase": "FROZEN" if refresh_due else "PREPARED",
               "challenger_checked": refresh_due,
               "challenger_count": len(shadow),
               "promotion_edge": comparison["promotion_edge"],
               "active_floor": comparison["active_floor"]}
    saved = store.save_live_session(session, cadence, watch["snapshot_id"],
                                    payload, frozen=refresh_due)
    return saved["candidates"], saved


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
                                           POLICY_ID, context, card,
                                           ttl_seconds=300 if mode == "live" else 86400)


def _monitor_rows(ideas: list[dict], quotes: dict[str, dict], *, mode: str,
                  in_window: bool, ib=None, account: str | None = None,
                  nlv: float = 100_000, available_funds: float | None = None,
                  account_symbols: set[str] | None = None,
                  session_usage: dict | None = None,
                  macro_gate: dict | None = None) -> list[dict]:
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
                card = make_stageable(card, idea, nlv, state, available_funds, overlap,
                                      session_usage=session_usage,
                                      macro_gate=macro_gate)
                if card["permitted"]:
                    card["candidate_id"] = _persist_card(
                        idea, card, account, mode, float(q["price"]), nlv, state)
            except Exception as exc:  # noqa: BLE001 — visible per ticker, monitor continues
                error = str(exc)
                card.update(permitted=False, tws_stage_allowed=False, status="WAIT",
                            blocks=[error])
        elif state["state"] == "TRIGGERED" and mode == "mock":
            card = make_stageable(card, idea, nlv, state, available_funds, overlap,
                                  session_usage=session_usage, macro_gate=macro_gate)
            if card["permitted"]:
                card["candidate_id"] = _persist_card(
                    idea, card, account or "MOCK-A", mode, float(q["price"]), nlv, state)
        rows.append({**visible_idea, "quote": q, "monitor": state, "trade_card": card,
                     "construction_error": error})
    return rows


def monitor(cadence: str = "daily", mode: str = "auto", account: str | None = None,
            nlv: float | None = None) -> dict:
    watch, caught_up = ensure_prepared(cadence, mode)
    try:
        session_age = (trading_today() - date.fromisoformat(watch["session"])).days
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("saved watchlist has an invalid market session; rescan") from exc
    max_age = 4
    if session_age < 0 or session_age > max_age:
        raise ValueError(f"saved {cadence} watchlist is stale ({session_age} calendar days); rescan")
    base_ideas = watch["candidates"]
    research = watch.get("research_pool") or base_ideas
    symbols = [x["symbol"] for x in research]
    errors = []
    actual_mode = mode
    gate = macro_risk_gate()
    selection_meta = None
    if mode in ("live", "auto"):
        try:
            def live_job(ib):
                profile = _account_profile(ib, account, nlv)
                q = quotes_tws(ib, symbols)
                ideas, live_selection = _live_selection(watch, q, cadence)
                account_symbols = _account_symbols(ib, profile["account"])
                usage = radar_store().entry_usage(profile["account"], trading_today().isoformat())
                return profile, live_selection, _monitor_rows(
                    ideas, q, mode="live", in_window=within_execution_window(),
                    ib=ib, account=profile["account"], nlv=profile["nlv"],
                    available_funds=profile.get("available_funds"),
                    account_symbols=account_symbols, session_usage=usage,
                    macro_gate=gate)
            profile, selection_meta, rows = with_ib(live_job)
            account, nlv = profile["account"], profile["nlv"]
            available_funds, cash = profile.get("available_funds"), profile.get("cash")
            actual_mode = "live"
        except Exception as exc:
            errors.append(f"live: {exc}")
            if mode == "live":
                raise RuntimeError(errors[-1]) from exc
    if actual_mode not in ("live",) or (mode == "auto" and errors):
        if mode == "mock":
            mock_seed = {x["symbol"]: {"price": x["price"], "fresh": True}
                         for x in research}
            ideas, selection_meta = _live_selection(watch, mock_seed, cadence)
            quotes = {}
            for i, idea in enumerate(ideas):
                trigger = float(idea["trigger"]["price"])
                px = trigger * (1.001 if idea["direction"] == "BULL" else .999) if i == 0 else idea["price"]
                quotes[idea["symbol"]] = {"price": px, "fresh": True,
                                           "source": "mock live", "captured_at": datetime.now(NY).isoformat()}
            usage = radar_store().entry_usage(account or "MOCK-A", trading_today().isoformat())
            rows = _monitor_rows(ideas, quotes, mode="mock", in_window=True,
                                 account=account or "MOCK-A", nlv=float(nlv or 100_000),
                                 available_funds=float(nlv or 100_000),
                                 session_usage=usage, macro_gate=gate)
            actual_mode = "mock"
            available_funds, cash = float(nlv or 100_000), float(nlv or 100_000)
        else:
            ideas = base_ideas[:ACTIVE_LIMIT]
            quotes = quotes_yf(symbols)
            rows = _monitor_rows(ideas, quotes, mode="yf", in_window=False,
                                 account=account, nlv=float(nlv or 100_000),
                                 macro_gate=gate)
            actual_mode = "yf"
            available_funds, cash = None, None
    clock = trading_clock()
    return {"policy_id": POLICY_ID, "mode": actual_mode,
            "cadence": cadence, "snapshot_id": watch["snapshot_id"],
            "session": watch["session"], "account": account, "nlv": nlv,
            "available_funds": available_funds, "cash": cash,
            "clock": clock, "standard_window": within_execution_window(),
            "upcoming_tier1": watch.get("upcoming_tier1", []),
            "macro_gate": gate, "caught_up": caught_up,
            "selection": selection_meta or {"phase": "BASELINE", "challenger_checked": False},
            "entry_usage": radar_store().entry_usage(account or "MOCK-A", clock["ny_date"]),
            "evidence": radar_store().evidence_summary(),
            "fallback_chain": errors, "candidates": rows,
            "triggered": sum(x["monitor"]["state"] == "TRIGGERED" for x in rows),
            "armed": sum(x["monitor"]["state"] == "ARMED" for x in rows),
            "safety": "Alerts never place orders. Stage buttons create transmit=False TWS combos only."}


def stage(candidate_id: str, quantity: int = 1) -> dict:
    checked = validate_for_stage(candidate_id, quantity)
    cand, stored, qty = checked["candidate"], checked["card"], checked["quantity"]
    if cand["policy_id"] != POLICY_ID:
        raise ValueError("candidate is not a Stock Opportunity Radar order")
    if cand["mode"] == "mock":
        result = {"orderId": -1, "status": "MockStaged", "transmit": False,
                  "candidate_id": candidate_id, "qty": qty, "legs": stored["legs_raw"],
                  "warnings": checked["warnings"]}
        campaign_store().record_order(candidate_id, qty, result)
        idea = cand["context"]["idea"]
        risk = float(stored.get("governor", {}).get("risk_per_lot") or
                     stored.get("cash_required") or 0) * qty
        radar_store().record_entry(candidate_id, cand["account"] or "MOCK-A",
                                   cand["context"]["session"], cand["symbol"],
                                   idea.get("cluster", cand["symbol"]), risk, qty, result)
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
        usage = radar_store().entry_usage(cand["account"], trading_today().isoformat())
        gate = macro_risk_gate()
        quote = quotes_tws(ib, [cand["symbol"]]).get(cand["symbol"])
        if not quote:
            raise RuntimeError("fresh underlying quote unavailable")
        trig = trigger_state(idea, quote["price"], fresh=True,
                             in_last_hour=within_execution_window())
        if trig["state"] != "TRIGGERED":
            raise ValueError("trigger no longer active inside 15:00–15:40 ET; order not staged")
        live = {**stored, "rationale": list(stored.get("rationale", [])),
                "legs_raw": [dict(x) for x in stored["legs_raw"]]}
        reprice_cards(ib, cand["symbol"], quote["price"], trading_today(), [live],
                      strict_option_liquidity=True)
        if live.get("mid_src") != "live" or live.get("liquidity", {}).get("flagged"):
            raise RuntimeError("exact option legs failed the refreshed NBBO liquidity check")
        width = abs(float(live["legs_raw"][1]["strike"]) - float(live["legs_raw"][0]["strike"]))
        if live["net_mid"] <= 0 or live["net_mid"] > width * .45:
            raise ValueError("refreshed debit no longer satisfies the 45%-of-width entry rule")
        sizing_nlv = min(float(ctx.get("nlv") or profile["nlv"]), profile["nlv"])
        checked_card = make_stageable(
            live, idea, sizing_nlv, trig, profile.get("available_funds"), False,
            session_usage=usage, macro_gate=gate)
        if not checked_card["permitted"]:
            raise ValueError("; ".join(checked_card["blocks"]))
        gov = checked_card["governor"]
        if qty > gov["approved_lots"]:
            raise ValueError(f"fresh risk budget approves {gov['approved_lots']} lots, requested {qty}")
        result = stage_suggestion(ib, cand["symbol"], live["legs_raw"], live["net_mid"],
                                  qty, transmit=False, account=cand["account"])
        result["radar_risk_amount"] = float(gov["risk_per_lot"]) * qty
        result["radar_cluster"] = idea.get("cluster", cand["symbol"])
        return result

    result = with_ib(live_job)
    result = {**result, "candidate_id": candidate_id, "transmit": False,
              "warnings": checked["warnings"]}
    campaign_store().record_order(candidate_id, qty, result)
    radar_store().record_entry(candidate_id, cand["account"], cand["context"]["session"],
                               cand["symbol"], result["radar_cluster"],
                               result["radar_risk_amount"], qty, result)
    return result


def evidence(refresh: bool = False) -> dict:
    store = radar_store()
    update = None
    if refresh and store.shadow_symbols():
        from core.stock_data import histories_yf
        update = store.update_outcomes(histories_yf(store.shadow_symbols()))
    return {"policy_id": POLICY_ID, "update": update,
            "summary": store.evidence_summary(),
            "false_breakout_definition":
                "Next trading-day close back through the trigger, or invalidation hit before target."}


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
