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


def stage_from_bars(bars: list[dict]) -> tuple[str, int]:
    """Weinstein stage from LONG-horizon structure — the 30-week line.

    core.regime's `trend` is a short-horizon EMA read tuned for index premium
    work. On NFLX it returned "UP" off a multi-day bounce while the weekly
    structure was unambiguously Stage 4, which is exactly the failure this
    engine exists to avoid. So the stage is computed here from price against a
    rising or falling 150-day line plus the sequence of highs, and regime.trend
    is not consulted at all.
    """
    closes = [b["close"] for b in bars]
    if len(closes) < 200:
        return "Stage unknown (insufficient history)", 0

    sma150 = sum(closes[-150:]) / 150.0
    prev150 = sum(closes[-170:-20]) / 150.0
    slope = (sma150 - prev150) / prev150 if prev150 else 0.0
    price = closes[-1]

    highs = [b["high"] for b in bars]
    # These are disjoint windows. Nested maxima make h13 <= h26 <= h52 true
    # almost by construction and incorrectly label an ordinary base Stage 4.
    recent_high = max(highs[-63:])
    middle_high = max(highs[-126:-63])
    older_high = max(highs[-252:-126]) if len(highs) >= 252 else max(highs[-189:-126])
    lower_highs = recent_high < middle_high < older_high
    recent_return = price / closes[-63] - 1.0 if closes[-63] else 0.0

    if price > sma150 and slope > 0.005 and not lower_highs:
        return "Stage 2 (advancing)", 1
    if price < sma150 and slope < -0.005 and lower_highs and recent_return < -0.03:
        return "Stage 4 (declining)", -1
    return "Stage 1/3 (basing or topping)", 0


def _stage(reg: dict, bars: list[dict] | None = None) -> tuple[str, int]:
    if bars:
        return stage_from_bars(bars)
    # No bars supplied: fall back to regime, but never claim Stage 2 from a
    # short-horizon read alone — an unconfirmed uptrend is treated as neutral.
    trend = (reg.get("trend") or "").lower()
    bias = reg.get("bias", 0)
    if "down" in trend and bias < 0:
        return "Stage 4 (declining)", -1
    return "Stage 1/3 (unconfirmed — no long-horizon bars)", 0


def iv_rv(reg: dict) -> tuple[float | None, float | None, float | None]:
    """Comparison IV and RV21 as percent; `iv30` is the legacy regime key."""
    iv = reg.get("iv30")
    rv = reg.get("rv21")
    if not iv or not rv:
        return iv, rv, None
    return iv, rv, iv / rv


def select(ctx: Context, hold: str = "medium", *,
           trend_state: int | None = None,
           trigger_fired: bool = False,
           bars: list[dict] | None = None) -> dict:
    """Return the Gate E decision payload for one ticker.

    `bars` are daily OHLC dicts (core.stock_data.histories_yf format). Supply
    them whenever available — without them the stage cannot be confirmed and
    the trend gate deliberately refuses to assert Stage 2.
    """
    reg = ctx.regime or {}
    stage_label, stage = _stage(reg, bars)
    if trend_state is not None:
        stage = trend_state
        stage_label = ({1: "Stage 2 (advancing)",
                        0: "Stage 1/3 (basing or topping)",
                        -1: "Stage 4 (declining)"}.get(stage, "Stage unknown"))

    iv, rv, ratio = iv_rv(reg)
    iv_rank = reg.get("iv_pctl")      # already 0-100 from core.regime
    # Single names have no free IV history, so core.yf_client sets ivp_proxy
    # and iv_pctl defaults to 50. A hard-coded 50 must never drive the matrix
    # or appear on a card as though it were measured.
    iv_proxy = bool(reg.get("ivp_proxy"))
    if iv_proxy:
        iv_rank = None

    blocks: list[str] = []
    notes: list[str] = []

    if ratio is None:
        blocks.append(
            "VOLATILITY DATA BLOCK: implied and realised volatility must both be "
            "measured before Gate E can compare buying with selling premium, so "
            "no structure is offered while either input is unavailable.")
    elif (ctx.data or {}).get("volatility_inputs_verified") is False:
        blocks.append(
            "VOLATILITY DATA BLOCK: the option surface used an unverified or "
            "historical fallback rather than contemporaneous cross-checked quotes, "
            "so its IV/RV comparison cannot authorize a structure.")

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
            f"PREMIUM-SELLING BLOCK: implied volatility of {iv:.1f}% is running "
            f"below thirty-day realised of {rv:.1f}%, giving a ratio of {ratio:.2f}, "
            f"so any short-premium structure would be selling volatility at a "
            f"steep discount to what the stock is actually delivering.")
        notes.append(
            "Long-premium structures are not blocked on volatility grounds here "
            "and are in fact favourably priced, because cheap implied volatility "
            "is exactly the condition that makes buying the long leg attractive.")
    if iv_proxy:
        notes.append(
            "IV rank is unavailable because there is no free implied-volatility "
            "history for single names, so the volatility read rests entirely on "
            "the IV-to-realised ratio rather than on where IV sits in its range.")

    if ratio is not None and ratio >= IVRV_FLOOR:
        notes.append(
            f"Implied volatility of {iv:.1f}% against realised of {rv:.1f}% gives "
            f"a ratio of {ratio:.2f}, so premium is fairly to richly priced and "
            f"short-premium structures are permitted.")

    # ------------------------------------------------------ selection matrix
    picks: list[str] = []
    if not blocks or all(b.startswith("PREMIUM") for b in blocks):
        low = iv_rank is not None and iv_rank < IV_RANK_LOW
        high = iv_rank is not None and iv_rank > IV_RANK_HIGH
        if iv_rank is None and ratio is not None:
            low, high = ratio < IVRV_FLOOR, ratio > 1.15

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

    # A "long" hold must actually have long-dated expiries on the surface.
    # core.chain.SCAN_DTE caps the default yfinance context at 85 DTE, so
    # without this check a months-long request silently returns a 28-day
    # structure — the exact mismatch that makes a card look right and be wrong.
    src = (ctx.data or {}).get("chain_source")
    if src == "IBKR TWS":
        notes.append(
            "The surface inputs came from TWS, but a structure is treated as "
            "model-priced unless every displayed leg carries its own verified "
            "quote provenance.")
    elif src == "yfinance":
        why = (ctx.data or {}).get("live_unavailable")
        notes.append(
            "Chain data came from yfinance"
            + (" because TWS was unreachable (%s)" % why if why else "")
            + "; each leg states whether IV was cross-checked from a quoted value "
              "or solved from bid/ask mid, while last-price fallbacks remain "
              "explicit and cannot authorize a ZEBRA leg.")

    shortfall = (ctx.data or {}).get("tenor_shortfall")
    if shortfall:
        req_lo, req_hi = shortfall["requested"]
        a_lo, a_hi = shortfall["available_dte"]
        blocks.append(
            f"TENOR BLOCK: the {hold} hold needs an expiry between {req_lo} and "
            f"{req_hi} days, but {ctx.symbol} lists nothing in that range - the "
            f"available expiries run {a_lo} to {a_hi} days.")
        blocks.append(
            "That is a listings fact rather than a data failure, so widening the "
            "search will not help; either pick a shorter hold or trade a name "
            "that has LEAPS listed.")
        picks = []
    elif slc is not None and not (lo <= slc.dte <= hi):
        blocks.append(
            f"TENOR BLOCK: the requested {hold} hold needs an expiry between {lo} "
            f"and {hi} days, but the nearest available on this surface is "
            f"{slc.dte} days, so no structure is offered rather than one built on "
            f"the wrong tenor.")
        picks = []

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
            "iv_rank_proxy": iv_proxy,
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

    lo, hi, _ = HOLDS.get(hold, HOLDS["medium"])
    out = select(ctx, hold, **kw)
    suggestions = []
    for key in out["structures"]:
        strat = REGISTRY.get(key)
        if not strat:
            continue
        if (key == "zebra"
                and (ctx.data or {}).get("chain_source") == "IBKR TWS"
                and not (ctx.data or {}).get("verified_zebra_quotes")):
            out["notes"].append(
                "ZEBRA was refused because TWS supplied no verified deep-in-the-money "
                "quote for the solved strike/expiry contract; an option-chain "
                "definition alone is not evidence that the leg is tradeable.")
            continue
        try:
            for sug in strat.propose(ctx):
                dtes = {(l.expiry - ctx.today).days for l in sug.legs}
                if not any(lo <= d <= hi for d in dtes):
                    out["notes"].append(
                        f"Dropped {sug.label} because its legs sit at "
                        f"{sorted(dtes)} days, outside the {lo}-{hi} day window "
                        f"the {hold} hold requires.")
                    continue
                item = sug.to_dict()
                if key == "zebra" and item.get("max_profit") == float("inf"):
                    item["max_profit"] = None
                    item["max_profit_unbounded"] = True
                item["price_provenance"] = ((item.get("evidence") or {})
                                             .get("leg_price_provenance")
                                             or ["model-derived from the context surface"])
                suggestions.append(item)
        except Exception as exc:
            out["notes"].append(
                f"Structure {key} could not be built for {ctx.symbol} "
                f"({type(exc).__name__}), so it has been dropped from the "
                f"shortlist rather than shown with partial data.")
    suggestions.sort(key=lambda s: s.get("score", 0), reverse=True)
    out["suggestions"] = suggestions
    # ACTION must name the structure the card actually shows. Ranking by score
    # can reorder the shortlist, and an action line that disagrees with the
    # legs below it is worse than no action line.
    if suggestions:
        out["action"] = suggestions[0]["strategy"].upper()
    elif out["eligible"]:
        out["action"] = "STAND ASIDE"
        out["eligible"] = False
    return out
