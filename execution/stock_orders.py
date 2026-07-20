"""Exact-chain construction and safe staging for stock-radar verticals."""
from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from core.ib_client import PARAMS_CACHE
from core.models import Leg
from core.pricing import risk_profile
from core.reprice import reprice_cards
from execution.optionstrat import optionstrat_url

NY = ZoneInfo("America/New_York")


def _nearest(values, target: float):
    return min(values, key=lambda x: abs(float(x) - float(target)))


def snap_vertical_live(ib, idea: dict, card: dict, spot: float,
                       today: date) -> dict:
    """Snap the model expiry/strikes to an actual TWS chain and reprice it."""
    from ib_insync import Option, Stock

    symbol = idea["symbol"]
    stock = Stock(symbol, "SMART", "USD")
    ib.qualifyContracts(stock)
    if not stock.conId:
        raise RuntimeError(f"{symbol}: underlying qualification failed")
    pkey = ("stock-radar-params", symbol)
    param = PARAMS_CACHE.get(pkey)
    if param is None:
        params = ib.reqSecDefOptParams(symbol, "", "STK", stock.conId)
        if not params:
            raise RuntimeError(f"{symbol}: TWS returned no option-chain parameters")
        param = next((p for p in params if p.exchange == "SMART"), params[0])
        PARAMS_CACHE.put(pkey, param)
    expiries = []
    for raw in param.expirations:
        try:
            d = datetime.strptime(str(raw)[:8], "%Y%m%d").date()
        except ValueError:
            continue
        if 40 <= (d - today).days <= 85:
            expiries.append(d)
    if not expiries:
        raise RuntimeError(f"{symbol}: no listed expiry between 40 and 85 DTE")
    wanted_exp = date.fromisoformat(card["legs_raw"][0]["expiry"])
    expiry = min(expiries, key=lambda d: abs((d - wanted_exp).days))
    strikes = sorted(float(x) for x in param.strikes
                     if spot * 0.65 <= float(x) <= spot * 1.35)
    if len(strikes) < 2:
        raise RuntimeError(f"{symbol}: usable strike list is empty")

    snapped = []
    for leg in card["legs_raw"]:
        strike = _nearest(strikes, float(leg["strike"]))
        snapped.append({**leg, "strike": float(strike), "expiry": expiry.isoformat(),
                        "trading_class": param.tradingClass})
    if snapped[0]["strike"] == snapped[1]["strike"]:
        idx = strikes.index(snapped[0]["strike"])
        direction = idea["direction"]
        j = min(idx + 1, len(strikes) - 1) if direction == "BULL" else max(idx - 1, 0)
        snapped[1]["strike"] = strikes[j]
    if snapped[0]["strike"] == snapped[1]["strike"]:
        raise RuntimeError(f"{symbol}: unable to form a non-zero-width vertical")

    # Qualify explicitly with the chain's trading class before the shared
    # repricer performs its own tolerant qualification/quote pass.
    opts = [Option(symbol, expiry.strftime("%Y%m%d"), x["strike"], x["cp"],
                   "SMART", tradingClass=param.tradingClass, currency="USD") for x in snapped]
    ib.qualifyContracts(*opts)
    if any(not x.conId for x in opts):
        raise RuntimeError(f"{symbol}: one or more vertical legs are unavailable")

    live = {**card, "legs_raw": snapped, "rationale": list(card.get("rationale", []))}
    reprice_cards(ib, symbol, spot, today, [live])
    if live.get("mid_src") != "live":
        raise RuntimeError(f"{symbol}: exact legs do not have a two-sided live quote")
    if live.get("liquidity", {}).get("flagged"):
        raise RuntimeError(f"{symbol}: option NBBO failed the liquidity gate")
    if live["net_mid"] <= 0:
        raise RuntimeError(f"{symbol}: debit vertical returned a non-debit midpoint")
    width = abs(snapped[1]["strike"] - snapped[0]["strike"])
    if live["net_mid"] > width * 0.45:
        raise RuntimeError(f"{symbol}: debit {live['net_mid']:.2f} exceeds 45% of {width:g}-point width")

    legs = [Leg(cp=x["cp"], strike=x["strike"], expiry=expiry, qty=x["qty"],
                iv=float(x.get("iv") or .30)) for x in snapped]
    live["legs"] = [x.key() for x in legs]
    live["risk_profile"] = risk_profile(spot, legs, today, entry=live["net_mid"])
    live["max_profit"] = live["risk_profile"]["max_profit"]
    live["max_loss"] = live["risk_profile"]["max_loss"]
    live["breakevens"] = live["risk_profile"]["breakevens"]
    live["cash_required"] = round(abs(live["max_loss"]) * 100, 2)
    live["optionstrat_url"] = optionstrat_url(symbol, snapped)
    live["label"] = (f"{snapped[0]['strike']:g}/{snapped[1]['strike']:g} "
                     f"{'call' if snapped[0]['cp'] == 'C' else 'put'} debit spread")
    return live


def approve_lots(card: dict, nlv: float | None, risk_pct: float = 0.005,
                 max_lots: int = 3, available_funds: float | None = None) -> dict:
    nlv = float(nlv or 100_000)
    risk = max(abs(float(card.get("max_loss") or 0)) * 100,
               float(card.get("cash_required") or 0), 1.0)
    budget = nlv * min(max(float(risk_pct), 0.001), 0.01)
    cash_binding = False
    if available_funds is not None:
        available_funds = max(float(available_funds), 0.0)
        cash_binding = available_funds < budget
        budget = min(budget, available_funds)
    lots = min(max_lots, int(budget // risk))
    binding = []
    if cash_binding:
        binding.append("available funds")
    if not lots:
        binding.append("per-trade structural risk")
    return {"approved_lots": lots, "risk_approved": lots > 0,
            "risk_per_lot": round(risk, 2), "risk_budget": round(budget, 2),
            "risk_pct_nlv": round(risk_pct * 100, 2),
            "available_funds": available_funds, "binding": binding}


def make_stageable(card: dict, idea: dict, nlv: float | None,
                   trigger: dict, available_funds: float | None = None,
                   position_overlap: bool = False) -> dict:
    card = dict(card)
    gov = approve_lots(card, nlv, available_funds=available_funds)
    event_status = idea.get("earnings", {}).get("status", "VERIFY")
    blocks = []
    if trigger.get("state") != "TRIGGERED":
        blocks.append("last-hour price trigger is not active")
    if event_status in ("VERIFY", "LOCKED"):
        blocks.append("earnings date is unverified or locked")
    if available_funds is None:
        blocks.append("IBKR AvailableFunds is unavailable")
    if position_overlap:
        blocks.append("an existing position or working order needs portfolio review")
    if not gov["risk_approved"]:
        blocks.append("risk budget approves zero spreads")
    card.update(governor=gov, blocks=blocks, permitted=not blocks,
                tws_stage_allowed=not blocks, status="ENTER" if not blocks else "WAIT",
                trigger=trigger, idea_id=idea.get("radar_candidate_id"),
                policy_id="stock-radar-v1")
    return card
