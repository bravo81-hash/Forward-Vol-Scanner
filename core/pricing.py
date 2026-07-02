"""Black-Scholes-Merton pricing, greeks, structure metrics. Pure math, no IO.

q = continuous dividend yield. Live cards are NBBO-repriced with TWS greeks,
so q drives mock mode, pre-reprice ranking, and the MODEL book greeks
(portfolio.book) — where an unmodelled SPX yield costs ~1 delta pt per ATM
contract at 30 DTE.
"""
from __future__ import annotations
import math
from datetime import date
from .models import Leg

RISK_FREE = 0.04

DIV_YIELD = {"SPX": 0.012, "SPY": 0.012, "NDX": 0.006, "QQQ": 0.006,
             "RUT": 0.011, "IWM": 0.011}


def q_for(symbol: str) -> float:
    return DIV_YIELD.get(symbol, 0.0)


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _d12(s, k, t, iv, r, q):
    sq = iv * math.sqrt(t)
    d1 = (math.log(s / k) + (r - q + 0.5 * iv * iv) * t) / sq
    return d1, d1 - sq, sq


def bs_price(s: float, k: float, t: float, iv: float, cp: str,
             r: float = RISK_FREE, q: float = 0.0) -> float:
    if t <= 0:
        return max(0.0, s - k if cp == "C" else k - s)
    if iv <= 0:
        iv = 0.01
    d1, d2, _ = _d12(s, k, t, iv, r, q)
    dq, dr = math.exp(-q * t), math.exp(-r * t)
    if cp == "C":
        return s * dq * norm_cdf(d1) - k * dr * norm_cdf(d2)
    return k * dr * norm_cdf(-d2) - s * dq * norm_cdf(-d1)


def bs_greeks(s: float, k: float, t: float, iv: float, cp: str,
              r: float = RISK_FREE, q: float = 0.0) -> dict:
    if t <= 0 or iv <= 0:
        itm = (s > k) if cp == "C" else (s < k)
        return {"delta": (1.0 if cp == "C" else -1.0) if itm else 0.0,
                "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1, d2, sq = _d12(s, k, t, iv, r, q)
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    dq, dr = math.exp(-q * t), math.exp(-r * t)
    delta = dq * norm_cdf(d1) if cp == "C" else dq * (norm_cdf(d1) - 1.0)
    core = -s * dq * pdf * iv / (2 * math.sqrt(t))
    if cp == "C":
        theta_yr = core + q * s * dq * norm_cdf(d1) - r * k * dr * norm_cdf(d2)
    else:
        theta_yr = core - q * s * dq * norm_cdf(-d1) + r * k * dr * norm_cdf(-d2)
    return {"delta": delta, "gamma": dq * pdf / (s * sq),
            "theta": theta_yr / 365.0, "vega": s * dq * pdf * math.sqrt(t) / 100.0}


def struct_value(spot: float, legs: list[Leg], today: date,
                 elapsed: int = 0, q: float = 0.0) -> float:
    v = 0.0
    for l in legs:
        t = max(0, (l.expiry - today).days - elapsed) / 365.0
        v += l.qty * bs_price(spot, l.strike, t, l.iv, l.cp, q=q)
    return v


def struct_greeks(spot: float, legs: list[Leg], today: date,
                  q: float = 0.0) -> dict:
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for l in legs:
        t = max(0, (l.expiry - today).days) / 365.0
        g = bs_greeks(spot, l.strike, t, l.iv, l.cp, q=q)
        for k in out:
            out[k] += l.qty * g[k]
    return {k: round(v, 4) for k, v in out.items()}


def struct_metrics(spot: float, legs: list[Leg], today: date,
                   entry: float | None = None, q: float = 0.0) -> dict:
    """Entry mid (model unless overridden with a live mid), max P/L and
    breakevens AT FRONT EXPIRY, hold-aware. Breakevens are linearly
    interpolated at the sign change (grid step is ~0.2% of spot — without
    interpolation, displayed BEs were off by up to half a step, ~6 SPX pts).
    Scan window always covers the strike envelope."""
    if entry is None:
        entry = struct_value(spot, legs, today, q=q)
    front = min((l.expiry - today).days for l in legs)
    max_p, max_l, bes = -1e18, 1e18, []
    lo = min(spot * 0.82, min(l.strike for l in legs) * 0.98)
    hi = max(spot * 1.18, max(l.strike for l in legs) * 1.02)
    step = spot * 0.002
    prev, prev_s = None, None
    s = lo
    while s <= hi:
        p = struct_value(s, legs, today, elapsed=front, q=q) - entry
        max_p, max_l = max(max_p, p), min(max_l, p)
        if prev is not None and (prev < 0) != (p < 0):
            bes.append(round(prev_s + (s - prev_s) * prev / (prev - p), 2))
        prev, prev_s = p, s
        s += step
    return {"entry": round(entry, 2), "max_profit": round(max_p, 2),
            "max_loss": round(max_l, 2), "breakevens": bes, "front_dte": front}
