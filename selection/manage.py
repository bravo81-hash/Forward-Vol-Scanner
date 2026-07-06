"""P2 — entry-time management plan attached to every suggestion card.

This is the plan you carry INTO OptionNet Explorer, not live management
(management stays in ONE by design). For each card it precomputes:

  * profit target / stop in $ per lot, family-typed (credit vs debit doctrine)
  * T+5 P&L at spot-flat and at +/-1 expected move  (struct_value elapsed=5)
  * P(short strike touched) within the planned hold, from RV21 (reflection法)
  * ATR-scaled adjustment trigger prices (the tested short strikes, with the
    ATR distance so the desk knows how far the tape must travel)
  * time stop = the gamma-week exit date (short leg must clear FRONT_EXIT_DTE)

All $ figures use the 100x index multiplier and are PER LOT.
"""
from __future__ import annotations

import math
from datetime import timedelta

from core.pricing import MULT, struct_value

HOLD_MAX = 10
FRONT_EXIT_DTE = 7

# family -> (profit_target_frac, stop_frac, basis)
#   credit basis: fracs are of credit received (PT 0.5 = buy back at half)
#   debit  basis: fracs are of debit paid    (PT 1.0 = double, SL 0.5 = -50%)
MGMT = {
    "condor":          (0.50, 2.00, "credit"),
    "bwb":             (0.50, 1.50, "credit"),
    "butterfly":       (1.00, 0.50, "debit"),
    "calendar":        (0.50, 0.50, "debit"),
    "double_calendar": (0.50, 0.50, "debit"),
    "diagonal":        (0.60, 0.50, "debit"),
}
_DEFAULT = (0.50, 1.00, "credit")


def _p_touch(spot: float, strike: float, rv21_pct: float, days: int) -> float:
    """P(underlying touches `strike` at least once within `days`), reflection
    principle on a driftless GBM: 2 * P(end beyond strike). rv21 in vol pts."""
    if strike <= 0 or spot <= 0 or days <= 0 or rv21_pct <= 0:
        return 0.0
    sigma = rv21_pct / 100 * math.sqrt(days / 252)
    if sigma <= 0:
        return 0.0
    d = abs(math.log(strike / spot)) / sigma
    return round(min(1.0, 2 * (1 - _norm_cdf(d))) * 100, 1)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def management_plan(ctx, s) -> dict:
    """Return the management dict for one Suggestion; pure/model-based."""
    pt_f, sl_f, basis = MGMT.get(s.strategy, _DEFAULT)
    net = s.net_mid                                   # +debit / -credit per spread
    credit = -net if net < 0 else 0.0
    debit = net if net > 0 else 0.0

    if basis == "credit" and credit > 0:
        pt_dollars = round(credit * pt_f * MULT, 0)          # profit = keep this much
        sl_dollars = round(-credit * sl_f * MULT, 0)         # loss cap
        pt_txt = f"buy back at {int((1 - pt_f) * 100)}% of credit"
        sl_txt = f"stop at {sl_f:g}x credit received"
    else:                                              # debit basis (or credit==0)
        base = debit if debit > 0 else abs(net)
        pt_dollars = round(base * pt_f * MULT, 0)
        sl_dollars = round(-base * sl_f * MULT, 0)
        pt_txt = f"take +{int(pt_f * 100)}% of debit"
        sl_txt = f"stop at -{int(sl_f * 100)}% of debit"
    # never let the stop exceed structural max loss
    if s.max_loss is not None:
        floor = round(s.max_loss * MULT, 0)
        sl_dollars = max(sl_dollars, floor)

    # ---- T+5 P&L (spot flat and +/-1 EM) ----
    front = min((l.expiry - ctx.today).days for l in s.legs)
    t5 = min(5, front)
    em5 = ctx.spot * ctx.regime["rv21"] / 100 * math.sqrt(t5 / 252) if t5 else 0.0
    entry = s.net_mid

    def pnl(px, elapsed):
        return round((struct_value(px, s.legs, ctx.today, elapsed=elapsed, q=ctx.q)
                      - entry) * MULT, 0)

    t5_pnl = {"flat": pnl(ctx.spot, t5),
              "up_em": pnl(ctx.spot + em5, t5),
              "dn_em": pnl(ctx.spot - em5, t5)}

    # ---- adjustment triggers: the short strikes, ATR-scaled ----
    atr = ctx.regime.get("atr") or (ctx.spot * ctx.regime["rv21"] / 100 / math.sqrt(252))
    shorts = sorted({l.strike for l in s.legs if l.qty < 0})
    triggers = []
    for k in shorts:
        dist = k - ctx.spot
        triggers.append({
            "price": k,
            "side": "up" if dist >= 0 else "down",
            "atr_away": round(abs(dist) / atr, 2) if atr else None,
            "p_touch_pct": _p_touch(ctx.spot, k, ctx.regime["rv21"], HOLD_MAX)})

    # ---- time stop ----
    fronts = [(l.expiry - ctx.today).days for l in s.legs if l.qty < 0]
    exit_by = (min(fronts) - FRONT_EXIT_DTE) if fronts else None
    exit_date = (ctx.today + timedelta(days=max(exit_by, 0))).isoformat() if exit_by is not None else None

    return {
        "pt_dollars": pt_dollars, "sl_dollars": sl_dollars,
        "pt_note": pt_txt, "sl_note": sl_txt, "basis": basis,
        "t5_pnl": t5_pnl, "em5_pts": round(em5, 1),
        "triggers": triggers,
        "time_stop": {"exit_in_days": exit_by, "exit_by": exit_date,
                      "rule": f"roll/close any short leg before {FRONT_EXIT_DTE} DTE"},
    }
