"""Term-structure / forward-vol / verdict math on a list of Slices."""
from __future__ import annotations
import math
from datetime import date
from .models import Slice
from .events import fomc_between, fomc_within

FRONT_DTE = (12, 21)
BACK_DTE = (26, 45)
MIN_GAP = 7

FOMC_HIST_MOVE_PCT = 0.9    # SPX FOMC-day |move| averages ~0.8-1.0%
HARVEST_MIN_RATIO = 1.25    # implied event move must clear hist by this ratio


def _iv_near(slices: list[Slice], dte: int) -> float:
    s = min(slices, key=lambda x: abs(x.dte - dte))
    return s.atm_iv


def iv_cm(slices: list[Slice], dte: int) -> float:
    """Constant-maturity ATM IV at `dte` — linear in TOTAL VARIANCE between
    the bracketing expiries, clamped at the ends. Nearest-slice substitution
    read "iv9" off whichever Friday happened to be closest (6d or 13d),
    labelling the term structure with the wrong maturities."""
    ss = sorted(slices, key=lambda s: s.dte)
    if dte <= ss[0].dte:
        return ss[0].atm_iv
    if dte >= ss[-1].dte:
        return ss[-1].atm_iv
    for a, b in zip(ss, ss[1:]):
        if a.dte <= dte <= b.dte:
            va, vb = a.atm_iv ** 2 * a.dte, b.atm_iv ** 2 * b.dte
            v = va + (vb - va) * (dte - a.dte) / (b.dte - a.dte)
            return math.sqrt(v / dte)
    return ss[-1].atm_iv


def forward_vol(iv1, t1, iv2, t2) -> float:
    if t2 <= t1:
        return float("nan")
    var = (iv2**2 * t2 - iv1**2 * t1) / (t2 - t1)
    return math.sqrt(var) if var > 0 else float("nan")


def pair_table(slices: list[Slice], today: date) -> list[dict]:
    rows = []
    for f in slices:
        if not FRONT_DTE[0] <= f.dte <= FRONT_DTE[1]:
            continue
        for b in slices:
            if not BACK_DTE[0] <= b.dte <= BACK_DTE[1] or b.dte - f.dte < MIN_GAP:
                continue
            spans_fomc = fomc_between(f.expiry, b.expiry)   # T3: warn, don't kill
            fv = forward_vol(f.atm_iv, f.dte / 365, b.atm_iv, b.dte / 365)
            if math.isnan(fv):
                continue
            rows.append({"front": f.expiry.isoformat(), "f_dte": f.dte,
                         "f_iv": round(f.atm_iv * 100, 2),
                         "back": b.expiry.isoformat(), "b_dte": b.dte,
                         "b_iv": round(b.atm_iv * 100, 2),
                         "fwd": round(fv * 100, 2),
                         "edge": round((f.atm_iv - fv) * 100, 2),
                         "fomc_in_front": fomc_within(f.expiry, today),
                         "fomc_between": spans_fomc})
    rows.sort(key=lambda r: r["edge"], reverse=True)
    return rows


def event_premium(slices: list[Slice], today: date, rv21: float) -> dict | None:
    """Implied FOMC-day move from the IV step across the event.

    f1 = listed expiry immediately BEFORE the next FOMC, f2 = first expiry
    ON/AFTER it. The variance f2 carries beyond f1, minus rv21 baseline for
    the non-event days between them, is attributed to the FOMC print.
    None when FOMC is > 21 days out or either side of the step is missing.
    """
    from .events import FOMC_2026
    fomc = next((d for d in FOMC_2026 if d >= today), None)
    if fomc is None or (fomc - today).days > 21:
        return None
    before = [s for s in slices if s.expiry < fomc]
    after = [s for s in slices if s.expiry >= fomc]
    if not before or not after:
        return None
    f1 = max(before, key=lambda s: s.expiry)
    f2 = min(after, key=lambda s: s.expiry)
    event_var = f2.atm_iv ** 2 * (f2.dte / 365) - f1.atm_iv ** 2 * (f1.dte / 365)
    baseline_var = (rv21 / 100) ** 2 * max(f2.dte - f1.dte - 1, 0) / 365
    implied = 100 * math.sqrt(max(event_var - baseline_var, 0))
    return {"f1": f1, "f2": f2, "implied_move_pct": round(implied, 2),
            "rich": implied >= HARVEST_MIN_RATIO * FOMC_HIST_MOVE_PCT}


def term_stats(slices: list[Slice]) -> dict:
    if len(slices) < 2:
        return {}
    iv9, iv30, iv45 = (iv_cm(slices, d) for d in (9, 30, 45))
    rat_f, rat_b = iv9 / iv30, iv30 / iv45
    rr30 = min(slices, key=lambda s: abs(s.dte - 30)).rr25
    skew_rich = rr30 / (iv30 * 100) > 0.30      # rr25 > 30% of ATM = steep
    verdict = ("INVERTED FRONT" if rat_f > 1.0 else
               "STEEP CONTANGO" if rat_b < 0.93 else
               "CONTANGO" if rat_b < 0.99 else "FLAT")
    return {"iv9": round(iv9 * 100, 2), "iv30": round(iv30 * 100, 2),
            "rat_front": round(rat_f, 3), "rat_back": round(rat_b, 3),
            "rr25_30d": round(rr30, 2), "skew_rich": skew_rich,
            "verdict": verdict}
