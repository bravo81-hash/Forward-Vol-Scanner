"""Gate E — single-name equity structure selector. Sibling to Gate S.

Two hard blocks carry most of the weight:

TREND BLOCK — a confirmed downtrend rejects every candidate regardless of how
attractive the volatility picture looks. A well-built structure on a broken
chart is an efficient way to be wrong.

PREMIUM-SELLING BLOCK — never sell premium when IV < RV. A beaten-down name
invites the "sell puts into the fear" reflex; NFLX at 31.9% IV against 51.2%
realised had no fear premium at all, only volatility offered at a 19-point
discount to what the stock was delivering.
"""
from __future__ import annotations

from core.models import Context

POLICY_ID = "gate-e-v1"

HOLDS = {"short": (7, 21, 14), "medium": (28, 60, 45), "long": (120, 400, 300)}

IV_RANK_LOW, IV_RANK_HIGH = 30.0, 70.0
IVRV_FLOOR = 1.0

LONG_PREMIUM = {"zebra", "diagonal", "debit_spread", "calendar", "double_calendar"}
SHORT_PREMIUM = {"condor", "bwb", "butterfly", "iron_fly", "call_bwb",
                 "put_bwb", "m3_bwb_call", "fly_variants", "balanced_put_fly",
                 "wide_otm_put_fly", "target_fly"}


def _stage(reg: dict) -> tuple[str, int]:
    """Weinstein-style stage from trend/bias already computed in core.regime."""
    trend = (reg.get("trend") or "").lower()
    bias = reg.get("bias", 0)
    adx = reg.get("adx", 0.0)
    if "up" in trend and bias > 0:
        return "Stage 2 (advancing)", 1
    if "down" in trend and bias < 0:
        return "Stage 4 (declining)", -1
    if adx and adx < 20:
        return "Stage 1/3 (basing or topping)", 0
    return "Stage 1/3 (indeterminate)", 0


def iv_rv(reg: dict) -> tuple[float | None, float | None, float | None]:
    iv = reg.get("iv30")
    rv = reg.get("rv21")
    if not iv or not rv:
        return iv, rv, None
    return iv, rv, iv / rv


def select(ctx: Context, hold: str = "medium", *,
           trend_state: int | None = None,
           trigger_fired: bool = False) -> dict:
    """Return the Gate E decision payload for one ticker."""
    reg = ctx.regime or {}
    stage_label, stage = _stage(reg)
    if trend_state is not None:
        stage = trend_state

    iv, rv, ratio = iv_rv(reg)
    iv_rank = reg.get("iv_pctl")
    iv_rank = iv_rank * 100 if iv_rank is not None and iv_rank <= 1 else iv_rank

    blocks: list[str] = []
    notes: list[str] = []

    # ---------------------------------------------------------- trend gate
    if stage < 0:
        blocks.append(
            f"TREND BLOCK: {ctx.symbol} is in a confirmed downtrend "
            f"({stage_label}), and the trend gate rejects all candidates in this "
            f"state regardless of the volatility picture, because a well-built "
            f"structure on a broken chart is an efficient way to be wrong.")

    if stage == 0 and not trigger_fired:
        blocks.append(
            "TRIGGER BLOCK: the name is basing but the reclaim trigger has not "
            "fired, so it stays on the watchlist; membership is not permission "
            "to trade.")

    # ------------------------------------------------ premium-selling gate
    sell_blocked = False
    if ratio is not None and ratio < IVRV_FLOOR:
        sell_blocked = True
        blocks.append(
            f"PREMIUM-SELLING BLOCK: implied volatility of {iv * 100:.1f}% is running "
            f"below thirty-day realised of {rv * 100:.1f}%, giving a ratio of {ratio:.2f}, "
            f"so any short-premium structure would be selling volatility at a "
            f"steep discount to what the stock is actually delivering.")
        notes.append(
            "Long-premium structures are not blocked on volatility grounds here "
            "and are in fact favourably priced, because cheap implied volatility "
            "is exactly the condition that makes buying the long leg attractive.")
    elif ratio is not None:
        notes.append(
            f"Implied volatility of {iv * 100:.1f}% against realised of {rv * 100:.1f}% gives "
            f"a ratio of {ratio:.2f}, so premium is fairly to richly priced and "
            f"short-premium structures are permitted.")

    # ------------------------------------------------------ selection matrix
    picks: list[str] = []
    if not blocks or all(b.startswith("PREMIUM") for b in blocks):
        low = iv_rank is not None and iv_rank < IV_RANK_LOW
        high = iv_rank is not None and iv_rank > IV_RANK_HIGH

        if stage > 0:
            if low or sell_blocked:
                picks = ["zebra", "diagonal"]
                notes.append(
                    "The trend is confirmed and premium is cheap, so the case is "
                    "for buying volatility through stock-replacement structures "
                    "rather than renting it out.")
            elif high and not sell_blocked:
                picks = ["debit_spread", "condor"]
                notes.append(
                    "The trend is confirmed and premium is rich, so a short put "
                    "spread or a call debit spread captures the move without "
                    "paying up for the long leg.")
            else:
                picks = ["diagonal", "zebra"]
                notes.append(
                    "The trend is confirmed with volatility in its middle range, "
                    "which is the diagonal's natural home: the short call is paid "
                    "rent on a grinding advance.")
        elif stage == 0 and trigger_fired:
            picks = ["debit_spread"] if (low or sell_blocked) else ["debit_spread", "bwb"]
            notes.append(
                "The base has produced a reclaim trigger, so a defined-risk debit "
                "structure participates in the resumption without assuming the "
                "trend is already established.")

    if sell_blocked:
        removed = [p for p in picks if p in SHORT_PREMIUM]
        picks = [p for p in picks if p not in SHORT_PREMIUM]
        if removed:
            notes.append(
                f"Removed {', '.join(removed)} from the shortlist because the "
                f"premium-selling block is active.")

    lo, hi, target = HOLDS.get(hold, HOLDS["medium"])
    slc = ctx.slice_near(target)

    action = "STAND ASIDE" if (blocks and not picks) else (
        f"{picks[0].upper()}" if picks else "STAND ASIDE")

    return {
        "policy_id": POLICY_ID,
        "symbol": ctx.symbol,
        "spot": ctx.spot,
        "hold": hold,
        "action": action,
        "stage": stage_label,
        "stage_code": stage,
        "inputs": {
            "iv30": iv, "rv21": rv, "iv_rv": round(ratio, 3) if ratio else None,
            "iv_rank": round(iv_rank, 1) if iv_rank is not None else None,
            "skew_rr25": round(slc.rr25, 2) if slc else None,
            "dte": slc.dte if slc else None,
            "target_dte": target,
        },
        "structures": picks,
        "blocks": blocks,
        "notes": notes,
        "eligible": bool(picks),
        "data": ctx.data,
    }


def build(ctx: Context, hold: str = "medium", **kw) -> dict:
    """Full payload: selection + concrete suggestions from the registry."""
    from strategies import REGISTRY

    out = select(ctx, hold, **kw)
    suggestions = []
    for key in out["structures"]:
        strat = REGISTRY.get(key)
        if not strat:
            continue
        try:
            suggestions += [s.to_dict() for s in strat.propose(ctx)]
        except Exception as exc:
            out["notes"].append(
                f"Structure {key} could not be built for {ctx.symbol} "
                f"({type(exc).__name__}), so it has been dropped from the "
                f"shortlist rather than shown with partial data.")
    suggestions.sort(key=lambda s: s.get("score", 0), reverse=True)
    out["suggestions"] = suggestions
    return out
