"""Deterministic campaign-management advice; never places an order."""
from __future__ import annotations


def advise_campaign(campaign: dict, mark: dict, context: dict | None = None) -> dict:
    card = campaign["card"]
    context = context or {}
    reasons, action = [], "HOLD"
    if not context.get("fresh", True):
        return {"action": "DATA INVALID", "reasons": ["market snapshot is stale"],
                "stage_allowed": False, "adjustments": []}
    pnl = float(mark.get("pnl_dollars") or 0.0)
    qty = max(int(campaign.get("quantity") or 1), 1)
    manage = card.get("manage", {})
    pt = float(manage.get("pt_dollars") or 0) * qty
    sl = float(manage.get("sl_dollars") or 0) * qty
    dte = mark.get("min_short_dte")
    delta = float(mark.get("delta") or 0.0)
    delta_limit = float(mark.get("delta_limit") or 999999)
    term = context.get("term") or context.get("regime", {}).get("term")
    stress_loss = float(mark.get("worst_stress_pnl") or 0.0)
    stress_limit = abs(float(mark.get("stress_limit") or 999999))

    if pt > 0 and pnl >= pt:
        action, reasons = "EXIT", [f"profit target reached (${pnl:.0f} >= ${pt:.0f})"]
    elif sl < 0 and pnl <= sl:
        action, reasons = "EXIT", [f"loss limit reached (${pnl:.0f} <= ${sl:.0f})"]
    elif dte is not None and int(dte) <= 7:
        action, reasons = "EXIT", [f"short leg is {int(dte)} DTE (gamma-week rule)"]
    elif term == "INVERTED FRONT" or stress_loss < -stress_limit:
        action = "REDUCE"
        reasons.append("de-gross condition: inverted front or stress limit breached")
    elif abs(delta) > delta_limit:
        action = "ADJUST"
        reasons.append(f"delta {delta:+.1f} exceeds ±{delta_limit:.1f}")
    elif campaign.get("state") == "ELIGIBLE":
        action, reasons = "REVIEW", ["test campaign created; verify shape in OptionNet Explorer"]
    else:
        reasons.append("no profit, time, stress, or Greek trigger")

    family = card.get("strategy")
    adjustments = {
        "bwb": ["close/reduce", "recenter BWB", "small call BWB on rally"],
        "m3_bwb_call": ["close/reduce", "roll ITM call up", "recenter BWB"],
        "balanced_fly": ["close/recenter; do not repair by default"],
        "iron_fly": ["close/reduce", "convert tested side to defined debit repair"],
        "otm_put_fly": ["close/recenter", "small call BWB on rally"],
        "call_bwb": ["close/reduce", "recenter call BWB"],
        "target_fly": ["close; no adjustment"],
        "debit_spread": ["close/reduce", "roll only if the new debit improves defined risk"],
        "fly_bull": ["close/reduce", "one whole-fly reset down above 14 DTE", "second breach exits"],
        "fly_chop": ["close/reduce", "one whole-fly recenter above 7 DTE", "second breach exits"],
        "fly_bear": ["close at stop/resistance", "no thesis repair or roll upward"],
        "timeedge": ["close/reduce", "one predefined same-width recenter", "next breach exits"],
        "timezone": ["reduce threatened PCS first", "one defense only", "next breach exits whole position"],
    }.get(family, ["close/reduce", "re-evaluate with fresh-entry gates"])
    return {"action": action, "reasons": reasons, "adjustments": adjustments,
            "stage_allowed": False, "policy_id": "campaign-management-v3",
            "evidence": card.get("evidence", {})}
