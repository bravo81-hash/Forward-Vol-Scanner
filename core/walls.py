"""Strike-level OI ladder: gamma walls, pin strike, GEX-sign proxy.

Live-only enrichment, run after the shortlist like NBBO repricing.
One pass over the cards' dominant short expiry: call+put open interest
(generic tick 101) across listed strikes within ~1.6x the expected move,
batched and cancelled. Results:
  * out["walls"]  — call/put wall strikes, max-OI pin, naive GEX sign
                    (OI x BS-gamma, dealers long calls / short puts)
  * card rationale flags — shorts parked on high-OI strikes, and a
    recenter hint when the OpEx pin fly body misses the max-OI strike.
Flags only — strikes are never silently moved.

TWS budget: <= MAX_STRIKES x 2 lines (~72), one extra batch pass.
"""
from __future__ import annotations

import math
from datetime import date, timedelta

from .chain import SURFACE_CFG
from .ib_client import MAX_LINES, PACE_S
from .pricing import bs_greeks

MAX_STRIKES = 36          # strikes laddered (x2 for C+P lines)
HOT_MULT = 2.5            # wall = strike OI >= HOT_MULT x median OI


def _oi(t):
    for f in ("callOpenInterest", "putOpenInterest", "openInterest"):
        v = getattr(t, f, None)
        if v is not None and not (isinstance(v, float) and math.isnan(v)) and v > 0:
            return int(v)
    return None


def _quote_oi(ib, contracts, timeout=6.0) -> dict[int, int]:
    """OI per conId. Own loop: tick 101 lags bid/ask, so wait on OI itself."""
    res: dict[int, int] = {}
    for i in range(0, len(contracts), MAX_LINES):
        batch = contracts[i:i + MAX_LINES]
        tickers = []
        for c in batch:
            tickers.append((c, ib.reqMktData(c, "101", snapshot=False)))
            ib.sleep(PACE_S)
        waited = 0.0
        while waited < timeout:
            ib.sleep(0.5)
            waited += 0.5
            if all(_oi(t) is not None for _, t in tickers):
                break
        for c, t in tickers:
            res[c.conId] = _oi(t) or 0
            ib.cancelMktData(c)
    return res


def scan_walls(ib, symbol: str, ctx, cards: list[dict]) -> dict | None:
    """Ladder the dominant short expiry of the shortlist; annotate cards."""
    shorts = [l for c in cards for l in c["legs_raw"] if l["qty"] < 0]
    if not shorts:
        return None
    exp_iso = max({l["expiry"] for l in shorts},
                  key=lambda e: sum(1 for l in shorts if l["expiry"] == e))
    dte = max((date.fromisoformat(exp_iso) - ctx.today).days, 1)
    em = ctx.spot * ctx.regime["rv21"] / 100 * math.sqrt(dte / 252)
    band = [k for k in ctx.strikes if abs(k - ctx.spot) <= 1.6 * em]
    while len(band) > MAX_STRIKES:
        band = band[::2]
    if not band:
        return None

    from ib_insync import Option
    _st, _exch, tc, is_idx = SURFACE_CFG[symbol]
    roots = [(tc, exp_iso.replace("-", ""))]
    d_exp = date.fromisoformat(exp_iso)
    if is_idx and d_exp.weekday() == 4 and 15 <= d_exp.day <= 21:
        # index monthly OI lives in the AM-settled class, keyed by its
        # last trade date = the Thursday before the third Friday
        roots.append((symbol, (d_exp - timedelta(days=1)).strftime("%Y%m%d")))
    specs = [(k, cp, Option(symbol, dt, k, cp, "SMART",
                            tradingClass=rt, currency="USD"))
             for k in band for cp in ("C", "P") for rt, dt in roots]
    ib.qualifyContracts(*[o for _, _, o in specs])
    oi_by_id = _quote_oi(ib, [o for _, _, o in specs if o.conId])

    slc = min(ctx.slices, key=lambda s: abs(s.dte - dte)) if ctx.slices else None
    iv = slc.atm_iv if slc else 0.18
    t_yr = dte / 365.0
    rows: dict[float, dict] = {}
    gex = 0.0
    for k, cp, o in specs:
        oi = oi_by_id.get(o.conId, 0)
        d = rows.setdefault(k, {"C": 0, "P": 0})
        d[cp] += oi                       # sum across roots (weekly + monthly)
        gex += oi * bs_greeks(ctx.spot, k, t_yr, iv, cp)["gamma"] * (1 if cp == "C" else -1)

    tot = {k: v["C"] + v["P"] for k, v in rows.items()}
    nz = sorted(v for v in tot.values() if v > 0)
    if not nz:
        return None
    call_wall = max(rows, key=lambda k: rows[k]["C"])
    put_wall = max(rows, key=lambda k: rows[k]["P"])
    pin = max(tot, key=lambda k: tot[k])
    hot = {k for k, v in tot.items() if v >= HOT_MULT * nz[len(nz) // 2]}
    hot.add(pin)

    for c in cards:
        for l in c["legs_raw"]:
            if l["qty"] < 0 and l["expiry"] == exp_iso and l["strike"] in hot:
                c["rationale"].append(
                    f"WALL: short {l['strike']:g}{l['cp']} sits on a high-OI "
                    f"strike ({tot[l['strike']]:,} OI) — pin/defense magnet")
        if c["strategy"] == "butterfly" and "Pin fly" in c["label"]:
            body = next((l["strike"] for l in c["legs_raw"] if l["qty"] < 0), None)
            if body is not None and body != pin:
                c["rationale"].append(
                    f"PIN: max-OI strike is {pin:g} ({tot[pin]:,} OI) — "
                    f"consider recentering body from {body:g}")

    return {"expiry": exp_iso, "n_strikes": len(band),
            "call_wall": {"strike": call_wall, "oi": rows[call_wall]["C"]},
            "put_wall": {"strike": put_wall, "oi": rows[put_wall]["P"]},
            "pin": {"strike": pin, "oi": tot[pin]},
            "gex_sign": "+" if gex > 0 else "-"}
