"""Implied volatility from option mid prices.

Yahoo serves placeholder implied volatility on long-dated equity options.
Measured on NVDA LEAPS: every strike carried a non-zero value, but the ATM
call read 1e-05 and the ATM put 4.98e-04 across the 217d, 307d and 399d
expiries. Those are floor values, not quotes, and core.yf_client correctly
rejected the slices - reporting the result as "0 expiries", which is a
statement about the wrong thing.

The chains themselves are fine (84 calls, 68 shared strikes, 30-460 span), so
the fix is to stop reading their IV column and solve it from the mid price.
"""
from __future__ import annotations

import math

from .pricing import RISK_FREE, bs_price

IV_LO, IV_HI = 0.01, 4.0
TOL, MAX_ITER = 1e-5, 60

# Yahoo's placeholders sit orders of magnitude below any real quote.
SANE_LO, SANE_HI = 0.02, 3.0


def is_sane(iv) -> bool:
    try:
        return SANE_LO < float(iv) < SANE_HI
    except (TypeError, ValueError):
        return False


def implied_vol(price: float, spot: float, strike: float, t: float, cp: str,
                q: float = 0.0, r: float = RISK_FREE) -> float | None:
    """Bisection solve. Returns None when the price admits no solution.

    Bisection rather than Newton: vega collapses for deep in-the-money LEAPS,
    which is exactly the region a ZEBRA long leg lives in, and Newton diverges
    there. Bisection is slower and cannot fail.
    """
    if price is None or price <= 0 or t <= 0 or spot <= 0 or strike <= 0:
        return None

    # No solution below intrinsic or above the trivial upper bound.
    disc_k = strike * math.exp(-r * t)
    disc_s = spot * math.exp(-q * t)
    intrinsic = max(0.0, disc_s - disc_k) if cp == "C" else max(0.0, disc_k - disc_s)
    if price < intrinsic - 1e-6:
        return None
    upper = disc_s if cp == "C" else disc_k
    if price >= upper:
        return None

    lo, hi = IV_LO, IV_HI
    lo_price = bs_price(spot, strike, t, lo, cp, q=q, r=r)
    hi_price = bs_price(spot, strike, t, hi, cp, q=q, r=r)
    if price < lo_price - TOL or price > hi_price + TOL:
        return None
    if abs(price - lo_price) <= TOL:
        return lo

    for _ in range(MAX_ITER):
        mid = 0.5 * (lo + hi)
        val = bs_price(spot, strike, t, mid, cp, q=q, r=r)
        if abs(val - price) < TOL:
            return mid
        if val < price:
            lo = mid
        else:
            hi = mid
        if hi - lo < TOL:
            break
    return 0.5 * (lo + hi)


def row_price(row) -> tuple[float | None, str | None]:
    """Return the usable price and its provenance."""
    def num(v):
        try:
            f = float(v)
            return f if f == f and f > 0 else None
        except (TypeError, ValueError):
            return None

    bid, ask = num(row.get("bid")), num(row.get("ask"))
    if bid and ask and ask >= bid:
        return 0.5 * (bid + ask), "bid/ask mid"
    last = num(row.get("lastPrice"))
    return (last, "lastPrice fallback") if last is not None else (None, None)


def row_mid(row) -> float | None:
    """Compatibility helper returning only the selected price."""
    return row_price(row)[0]


def repair_iv(frame, spot: float, t: float, cp: str, q: float = 0.0):
    """Return a copy of a yfinance chain frame with impliedVolatility solved.

    Rows whose quoted IV is already sane are left alone, so this is a no-op on
    the near-dated expiries where Yahoo's numbers are real.
    """
    if frame is None or getattr(frame, "empty", True):
        return frame
    out = frame.copy()
    solved, sources, cross_checked = [], [], []
    for _, row in out.iterrows():
        quoted = row.get("impliedVolatility")
        price, source = row_price(row)
        bid = row.get("bid")
        ask = row.get("ask")
        try:
            bid, ask = float(bid), float(ask)
            valid_market = (bid == bid and ask == ask and bid > 0 and ask >= bid)
        except (TypeError, ValueError):
            valid_market = False

        trusted = False
        if is_sane(quoted) and valid_market:
            model = bs_price(spot, float(row["strike"]), t, float(quoted), cp,
                             q=q, r=RISK_FREE)
            # Permit a small rounding cushion, but never trust a quoted IV whose
            # model value lies outside the contemporaneous market.
            cushion = max(0.01, 0.02 * (ask - bid))
            trusted = bid - cushion <= model <= ask + cushion

        if trusted:
            solved.append(float(quoted))
            sources.append("quoted IV cross-checked to bid/ask")
            cross_checked.append(True)
            continue

        iv = implied_vol(price, spot, float(row["strike"]), t, cp, q=q)
        solved.append(iv if iv is not None else 0.0)
        sources.append(source or "unpriced")
        cross_checked.append(source == "bid/ask mid" and iv is not None)
    out["impliedVolatility"] = solved
    out["equityIvPriceSource"] = sources
    out["equityIvCrossChecked"] = cross_checked
    return out
