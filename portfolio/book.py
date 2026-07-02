"""Live book per symbol from TWS positions; greeks via model pricing."""
from __future__ import annotations
from datetime import date, datetime

from core.chain import iv_at
from core.models import Context, Leg
from core.pricing import struct_greeks, struct_value

CAMPAIGN_MAX_DTE = 60   # legs beyond this belong to the separate long-DTE
                        # campaign (130/160/200 DTE) and must not drive this
                        # app's greeks, budgets, or fit scores

STRESS_SCENARIOS = [("-5% spot / IV+10 / 2d", -0.05, 0.10, 2),
                    ("-2% spot / IV+4 / 1d",  -0.02, 0.04, 1),
                    ("+3% spot / IV-3 / 2d",  +0.03, -0.03, 2)]


def fetch_positions(ib, symbol: str, account: str | None = None) -> list[dict]:
    out = []
    for p in ib.positions():
        if account and p.account != account:
            continue
        c = p.contract
        if c.secType in ("OPT", "FOP") and c.symbol == symbol and p.position:
            out.append({"cp": c.right, "strike": float(c.strike),
                        "expiry": c.lastTradeDateOrContractMonth,
                        "qty": int(p.position), "conId": c.conId})
    return out


def _position_legs(ctx: Context, positions: list[dict]) -> list[Leg]:
    legs = []
    for p in positions:
        exp = datetime.strptime(p["expiry"][:8], "%Y%m%d").date()
        slc = min(ctx.slices, key=lambda s: abs((s.expiry - exp).days)) if ctx.slices else None
        iv = iv_at(slc, p["strike"]) if slc else 0.18
        legs.append(Leg(cp=p["cp"], strike=p["strike"], expiry=exp,
                        qty=p["qty"], iv=iv))
    return legs


def book_greeks(ctx: Context, positions: list[dict]) -> dict:
    all_legs = _position_legs(ctx, positions)
    legs = [l for l in all_legs
            if (l.expiry - ctx.today).days <= CAMPAIGN_MAX_DTE]
    g = struct_greeks(ctx.spot, legs, ctx.today, q=ctx.q) if legs else \
        {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    fronts = [max(0, (l.expiry - ctx.today).days) for l in legs if l.qty < 0]
    return {"positions": len(positions), "greeks": g,
            "min_short_dte": min(fronts) if fronts else None,
            "gamma_flag": bool(fronts) and min(fronts) <= 7,
            "excluded_long_dte": len(all_legs) - len(legs)}


def stress_book(ctx: Context, positions: list[dict],
                scenarios=STRESS_SCENARIOS) -> list[dict]:
    """Shock the FULL book (campaign legs included — the stress test is the
    one place the whole position list matters). $ P&L per scenario."""
    legs = _position_legs(ctx, positions)
    if not legs:
        return []
    base = struct_value(ctx.spot, legs, ctx.today, q=ctx.q)
    out = []
    for name, ds, div, days in scenarios:
        shocked = [Leg(cp=l.cp, strike=l.strike, expiry=l.expiry,
                       qty=l.qty, iv=max(l.iv + div, 0.01)) for l in legs]
        v = struct_value(ctx.spot * (1 + ds), shocked, ctx.today, elapsed=days, q=ctx.q)
        out.append({"name": name, "pnl": round((v - base) * 100, 0)})
    return out
