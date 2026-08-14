"""Context + bars for the equity LEAPS engine, with the right DTE window.

The default yfinance context caps expiries at SCAN_DTE (5, 85) days, which is
correct for index premium work and useless for LEAPS. Requesting a months-long
hold against that surface silently returns a four-week structure — a card that
looks right and is wrong. So the window is derived from the hold here, once.
"""
from __future__ import annotations

from datetime import date

from core.models import Context

# Widened windows per hold. The lower bound stays generous because single-name
# LEAPS chains are sparse and an exact target often is not listed.
WINDOWS = {"short": (3, 45), "medium": (14, 120), "long": (90, 500)}


def window_for(hold: str) -> tuple[int, int]:
    return WINDOWS.get(hold, WINDOWS["medium"])


def build(symbol: str, hold: str = "medium", *,
          today: date | None = None) -> tuple[Context, list[dict]]:
    """Return (context, daily bars). Raises RuntimeError if unusable."""
    from core.stock_data import histories_yf
    from core.yf_client import build_context_yf

    ctx = build_context_yf(symbol, today=today, dte_range=window_for(hold))
    bars = histories_yf([symbol], period="2y").get(symbol, [])
    return ctx, bars
