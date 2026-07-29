"""Exact-contract TWS validation for a shortlisted value-entry put.

This module does not stage or transmit an order.  It requests live/frozen
quotes and an account-specific what-if margin estimate only.
"""
from __future__ import annotations

import math

from core.ib_client import quote_many


def _valid_price(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) and out >= 0 else None


def _margin_number(value) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def validate_candidate_tws(ib, symbol: str, candidate: dict,
                           account: str | None = None) -> dict:
    from ib_insync import ComboLeg, Contract, LimitOrder, Option

    symbol = symbol.upper().strip()
    expiry = str(candidate["expiry"]).replace("-", "")
    short = Option(symbol, expiry, float(candidate["strike"]), "P", "SMART",
                   currency="USD")
    contracts = [short]
    long = None
    if candidate.get("long_strike"):
        long = Option(symbol, expiry, float(candidate["long_strike"]), "P", "SMART",
                      currency="USD")
        contracts.append(long)
    ib.qualifyContracts(*contracts)
    if any(not contract.conId for contract in contracts):
        raise RuntimeError(f"{symbol}: exact option contract qualification failed")
    quotes = quote_many(ib, contracts, fields="100,101", want_greeks=True)
    short_q = quotes.get(short.conId) or {}
    bid, ask = _valid_price(short_q.get("bid")), _valid_price(short_q.get("ask"))
    if bid is None or ask is None or ask <= 0 or ask < bid:
        raise RuntimeError(f"{symbol}: short put has no usable two-sided TWS quote")
    short_credit = round(bid + .25 * (ask - bid), 2)
    long_cost = 0.0
    long_q = {}
    if long:
        long_q = quotes.get(long.conId) or {}
        long_bid = _valid_price(long_q.get("bid"))
        long_ask = _valid_price(long_q.get("ask"))
        if long_bid is None or long_ask is None or long_ask <= 0 or long_ask < long_bid:
            raise RuntimeError(f"{symbol}: protective put has no usable two-sided TWS quote")
        long_cost = long_ask
    net_credit = round(short_credit - long_cost, 2)
    if net_credit <= 0:
        raise RuntimeError(f"{symbol}: exact option quotes do not produce a net credit")

    if long:
        bag = Contract(symbol=symbol, secType="BAG", currency="USD", exchange="SMART")
        bag.comboLegs = [
            ComboLeg(conId=short.conId, ratio=1, action="SELL", exchange="SMART"),
            ComboLeg(conId=long.conId, ratio=1, action="BUY", exchange="SMART"),
        ]
        order_contract = bag
        order = LimitOrder("SELL", 1, net_credit)
    else:
        order_contract = short
        order = LimitOrder("SELL", 1, short_credit)
    if account:
        order.account = account
    order.transmit = False
    margin = ib.whatIfOrder(order_contract, order)
    iv = short_q.get("iv")
    delta = (short_q.get("greeks") or {}).get("delta")
    return {
        "symbol": symbol, "source": "TWS live/frozen", "account": account,
        "expiry": candidate["expiry"], "strike": float(candidate["strike"]),
        "long_strike": float(candidate["long_strike"]) if long else None,
        "bid": round(bid, 2), "ask": round(ask, 2),
        "executable_credit": short_credit, "long_cost": round(long_cost, 2),
        "net_credit": net_credit, "net_basis": round(float(candidate["strike"]) - short_credit, 2),
        "iv_pct": round(float(iv) * 100, 1) if iv is not None else None,
        "delta": round(abs(float(delta)), 3) if delta is not None else None,
        "open_interest": short_q.get("oi"), "volume": short_q.get("volume"),
        "what_if": {
            "initial_margin_change": _margin_number(
                getattr(margin, "initMarginChange", None)),
            "maintenance_margin_change": _margin_number(
                getattr(margin, "maintMarginChange", None)),
            "equity_with_loan_change": _margin_number(
                getattr(margin, "equityWithLoanChange", None)),
            "warning": getattr(margin, "warningText", "") or None,
        },
        "transmitted": False, "staged": False,
    }
