"""Black-Scholes pricing, greeks, structure metrics. Pure math, no IO."""
from __future__ import annotations
import math
from datetime import date
from .models import Leg

RISK_FREE = 0.04


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(s: float, k: float, t: float, iv: float, cp: str, r: float = RISK_FREE) -> float:
    if t <= 0:
        return max(0.0, s - k if cp == "C" else k - s)
    if iv <= 0:
        iv = 0.01
    sq = iv * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * iv * iv) * t) / sq
    d2 = d1 - sq
    if cp == "C":
        return s * norm_cdf(d1) - k * math.exp(-r * t) * norm_cdf(d2)
    return k * math.exp(-r * t) * norm_cdf(-d2) - s * norm_cdf(-d1)


def bs_greeks(s: float, k: float, t: float, iv: float, cp: str, r: float = RISK_FREE) -> dict:
    if t <= 0 or iv <= 0:
        itm = (s > k) if cp == "C" else (s < k)
        return {"delta": (1.0 if cp == "C" else -1.0) if itm else 0.0,
                "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    sq = iv * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * iv * iv) * t) / sq
    d2 = d1 - sq
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2 * math.pi)
    delta = norm_cdf(d1) if cp == "C" else norm_cdf(d1) - 1.0
    theta_yr = -s * pdf * iv / (2 * math.sqrt(t)) - (
        r * k * math.exp(-r * t) * (norm_cdf(d2) if cp == "C" else -norm_cdf(-d2)))
    return {"delta": delta, "gamma": pdf / (s * sq),
            "theta": theta_yr / 365.0, "vega": s * pdf * math.sqrt(t) / 100.0}


def struct_value(spot: float, legs: list[Leg], today: date, elapsed: int = 0) -> float:
    v = 0.0
    for l in legs:
        t = max(0, (l.expiry - today).days - elapsed) / 365.0
        v += l.qty * bs_price(spot, l.strike, t, l.iv, l.cp)
    return v


def struct_greeks(spot: float, legs: list[Leg], today: date) -> dict:
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for l in legs:
        t = max(0, (l.expiry - today).days) / 365.0
        g = bs_greeks(spot, l.strike, t, l.iv, l.cp)
        for k in out:
            out[k] += l.qty * g[k]
    return {k: round(v, 4) for k, v in out.items()}


def struct_metrics(spot: float, legs: list[Leg], today: date,
                   entry: float | None = None) -> dict:
    """Entry mid (model unless overridden with a live mid), max P/L and
    breakevens AT FRONT EXPIRY, hold-aware."""
    if entry is None:
        entry = struct_value(spot, legs, today)
    front = min((l.expiry - today).days for l in legs)
    max_p, max_l, bes, prev = -1e18, 1e18, [], None
    lo, hi = spot * 0.82, spot * 1.18
    s = lo
    while s <= hi:
        p = struct_value(s, legs, today, elapsed=front) - entry
        max_p, max_l = max(max_p, p), min(max_l, p)
        if prev is not None and (prev < 0) != (p < 0):
            bes.append(round(s, 1))
        prev = p
        s += spot * 0.002
    return {"entry": round(entry, 2), "max_profit": round(max_p, 2),
            "max_loss": round(max_l, 2), "breakevens": bes, "front_dte": front}
