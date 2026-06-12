"""Live book per symbol from TWS positions; greeks via model pricing."""
from __future__ import annotations
from datetime import date, datetime

from core.chain import iv_at
from core.models import Context, Leg
from core.pricing import struct_greeks


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


def book_greeks(ctx: Context, positions: list[dict]) -> dict:
    legs = []
    for p in positions:
        exp = datetime.strptime(p["expiry"][:8], "%Y%m%d").date()
        slc = min(ctx.slices, key=lambda s: abs((s.expiry - exp).days)) if ctx.slices else None
        iv = iv_at(slc, p["strike"]) if slc else 0.18
        legs.append(Leg(cp=p["cp"], strike=p["strike"], expiry=exp,
                        qty=p["qty"], iv=iv))
    g = struct_greeks(ctx.spot, legs, ctx.today) if legs else \
        {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    fronts = [max(0, (l.expiry - ctx.today).days) for l in legs if l.qty < 0]
    return {"positions": len(positions), "greeks": g,
            "min_short_dte": min(fronts) if fronts else None,
            "gamma_flag": bool(fronts) and min(fronts) <= 7}
