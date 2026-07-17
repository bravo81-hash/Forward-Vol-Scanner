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
MULT = 100    # index option contract multiplier — the per-unit -> Risk-Navigator
              # conversion factor (underlying deltas, $/day theta, $/vol-pt vega)

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
    for leg in legs:
        t = max(0, (leg.expiry - today).days - elapsed) / 365.0
        v += leg.qty * bs_price(spot, leg.strike, t, leg.iv, leg.cp, q=q)
    return v


def struct_greeks(spot: float, legs: list[Leg], today: date,
                  q: float = 0.0) -> dict:
    out = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for leg in legs:
        t = max(0, (leg.expiry - today).days) / 365.0
        g = bs_greeks(spot, leg.strike, t, leg.iv, leg.cp, q=q)
        for k in out:
            out[k] += leg.qty * g[k]
    return {k: round(v, 4) for k, v in out.items()}


def struct_metrics(spot: float, legs: list[Leg], today: date,
                   entry: float | None = None, q: float = 0.0) -> dict:
    """Entry mid (model unless overridden with a live mid), max P/L and
    breakevens AT FRONT EXPIRY, hold-aware. Single-expiry extrema include
    every exact strike; time-spread extrema use a fine grid plus strike knots.
    The scan window extends beyond the full strike envelope."""
    if entry is None:
        # Cards and executable limits use cents, so the risk profile must use
        # the same rounded entry rather than an undisplayed theoretical value.
        entry = round(struct_value(spot, legs, today, q=q), 2)
    front = min((leg.expiry - today).days for leg in legs)
    multi_expiry = len({leg.expiry for leg in legs}) > 1
    # A single-expiry payoff is piecewise linear, so strikes are the exact
    # extrema.  A calendar/hybrid still owns later-dated option value at the
    # front expiry; scan it finely and include every strike as a knot.
    samples = 4001 if multi_expiry else 2
    xs, pnl = _profile_values(spot, legs, today, entry, q, front, samples)
    bes = _zero_crossings(xs, pnl)
    return {"entry": round(entry, 2), "max_profit": round(max(pnl), 2),
            "max_loss": round(min(pnl), 2), "breakevens": bes,
            "front_dte": front}


def risk_profile(spot: float, legs: list[Leg], today: date,
                 entry: float | None = None, q: float = 0.0,
                 samples: int = 241) -> dict:
    """Return a graph-ready, hold-aware risk profile in option-price points.

    For one-expiry structures the solid curve is the exact expiration payoff.
    For calendars and the TimeZone hybrid it is the front-expiry mark with the
    back option valued at its remaining tenor and its entry IV held constant.
    This is the same model and entry price used by ``struct_metrics``.
    """
    if entry is None:
        entry = round(struct_value(spot, legs, today, q=q), 2)
    front = min((leg.expiry - today).days for leg in legs)
    multi_expiry = len({leg.expiry for leg in legs}) > 1
    xs, front_pnl = _profile_values(spot, legs, today, entry, q, front, samples)
    _, t5_pnl = _profile_values(spot, legs, today, entry, q, min(5, front), samples,
                                xs=xs)
    metrics = struct_metrics(spot, legs, today, entry=entry, q=q)
    return {
        "x": [round(x, 2) for x in xs],
        "front_expiry": [round(p, 2) for p in front_pnl],
        "t5": [round(p, 2) for p in t5_pnl],
        "entry": round(entry, 2),
        "front_dte": front,
        "multi_expiry": multi_expiry,
        "curve_label": "front-expiry model" if multi_expiry else "expiration payoff",
        "assumption": ("Back-leg IV held at entry; confirm the live surface in OptionStrat/ONE."
                       if multi_expiry else "Exact defined-risk expiration payoff."),
        "max_profit": metrics["max_profit"],
        "max_loss": metrics["max_loss"],
        "breakevens": metrics["breakevens"],
    }


def _profile_values(spot: float, legs: list[Leg], today: date, entry: float,
                    q: float, elapsed: int, samples: int,
                    xs: list[float] | None = None) -> tuple[list[float], list[float]]:
    if xs is None:
        lo = min(spot * 0.70, min(leg.strike for leg in legs) * 0.90)
        hi = max(spot * 1.30, max(leg.strike for leg in legs) * 1.10)
        grid = [lo + (hi - lo) * i / max(samples - 1, 1) for i in range(samples)]
        xs = sorted(set(grid + [float(leg.strike) for leg in legs]))
    pnl = [struct_value(x, legs, today, elapsed=elapsed, q=q) - entry for x in xs]
    return xs, pnl


def _zero_crossings(xs: list[float], ys: list[float]) -> list[float]:
    roots = []
    for i in range(1, len(xs)):
        x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
        if abs(y0) < 1e-10:
            roots.append(x0)
        elif y0 * y1 < 0:
            roots.append(x0 + (x1 - x0) * (-y0) / (y1 - y0))
    if abs(ys[-1]) < 1e-10:
        roots.append(xs[-1])
    out = []
    for root in roots:
        rounded = round(root, 2)
        if not out or abs(rounded - out[-1]) > 0.02:
            out.append(rounded)
    return out
