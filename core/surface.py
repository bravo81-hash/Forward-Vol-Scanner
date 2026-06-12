"""Term-structure / forward-vol / verdict math on a list of Slices."""
from __future__ import annotations
import math
from datetime import date
from .models import Slice
from .events import fomc_between, fomc_within

FRONT_DTE = (12, 21)
BACK_DTE = (26, 45)
MIN_GAP = 7


def _iv_near(slices: list[Slice], dte: int) -> float:
    s = min(slices, key=lambda x: abs(x.dte - dte))
    return s.atm_iv


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
            if fomc_between(f.expiry, b.expiry):
                continue                      # never buy the event in the back only
            fv = forward_vol(f.atm_iv, f.dte / 365, b.atm_iv, b.dte / 365)
            if math.isnan(fv):
                continue
            rows.append({"front": f.expiry.isoformat(), "f_dte": f.dte,
                         "f_iv": round(f.atm_iv * 100, 2),
                         "back": b.expiry.isoformat(), "b_dte": b.dte,
                         "b_iv": round(b.atm_iv * 100, 2),
                         "fwd": round(fv * 100, 2),
                         "edge": round((f.atm_iv - fv) * 100, 2),
                         "fomc_in_front": fomc_within(f.expiry, today)})
    rows.sort(key=lambda r: r["edge"], reverse=True)
    return rows


def term_stats(slices: list[Slice]) -> dict:
    if len(slices) < 2:
        return {}
    iv9, iv30, iv45 = (_iv_near(slices, d) for d in (9, 30, 45))
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
