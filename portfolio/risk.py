"""Per-surface risk budgets + portfolio-fit scoring.

Budgets are per 1-lot book in greek units; tune in config below.
fit_score rewards a suggestion that moves book vega/delta TOWARD the
target band and penalises one that piles on in the same direction.
"""
from __future__ import annotations
from core.models import Suggestion

BUDGET = {            # |limit| per surface, per account unit
    "vega": 12.0,     # net vega (per 1 vol pt, per spread-lot units)
    "delta": 0.30,    # net delta
    "theta_min": 0.0, # net theta should stay >= 0 for a harvest book
}


def fit_score(book: dict, s: Suggestion) -> float:
    if not book or "greeks" not in book:
        return 0.0
    bg, sg = book["greeks"], s.greeks
    score = 0.0
    for k, lim in (("vega", BUDGET["vega"]), ("delta", BUDGET["delta"])):
        before, after = bg[k], bg[k] + sg[k]
        if abs(after) > lim:
            score -= 1.0 * (abs(after) - lim) / lim          # busts budget
        elif abs(after) < abs(before):
            score += 0.3                                      # pulls toward 0
        elif abs(after) > abs(before) and abs(before) > lim * 0.6:
            score -= 0.3                                      # piles on
    if bg["theta"] + sg["theta"] < BUDGET["theta_min"]:
        score -= 0.4
    return round(score, 3)


def book_warnings(book: dict) -> list[str]:
    if not book or "greeks" not in book:
        return []
    g, out = book["greeks"], []
    if abs(g["vega"]) > BUDGET["vega"]:
        out.append(f"Book vega {g['vega']:+.1f} exceeds ±{BUDGET['vega']:.0f} budget")
    if abs(g["delta"]) > BUDGET["delta"]:
        out.append(f"Book delta {g['delta']:+.2f} exceeds ±{BUDGET['delta']:.2f}")
    if g["theta"] < BUDGET["theta_min"]:
        out.append("Book theta negative — not a harvest book")
    if book.get("gamma_flag"):
        out.append(f"Short leg at {book['min_short_dte']} DTE — gamma-week exit rule triggered")
    return out
