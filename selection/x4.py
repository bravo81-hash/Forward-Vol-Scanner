"""Live X4 strategy construction on top of the shared FVS market context.

The public X4 material describes the posture of V14/V17/V22, but not a
complete mechanical strike-selection algorithm.  This module therefore makes
the translation explicit and inspectable: the app selects listed strikes from
the live TWS surface, publishes the assumptions, and keeps every candidate in
manual-review / hypothesis status.
"""
from __future__ import annotations

import math
from datetime import date

from core.models import Context, Leg, Suggestion
from core.pricing import MULT, struct_greeks, struct_metrics

POLICY_ID = "x4-live-blueprint-v1"
STRATEGY_ORDER = ("v14", "v17", "v22")


def _iv_expected_move(ctx: Context, dte: int, iv: float) -> float:
    return ctx.spot * max(iv, .01) * math.sqrt(max(dte, 1) / 365)


PREFERRED_DTE = (60, 85)
FALLBACK_DTE = (30, 85)
TARGET_DTE = 77


def _slice(ctx: Context):
    """Nearest listed expiry to the X4 tenor.

    Returns (slice, fell_back). The fallback to 30-85 DTE materially changes
    the character of the structure — a 35 DTE "V22" is not the published
    posture — so the caller reports it rather than substituting silently.
    """
    preferred = [s for s in ctx.slices
                 if PREFERRED_DTE[0] <= s.dte <= PREFERRED_DTE[1]]
    fallback = [s for s in ctx.slices
                if FALLBACK_DTE[0] <= s.dte <= FALLBACK_DTE[1]]
    pool = preferred or fallback
    if not pool:
        return None, False
    return min(pool, key=lambda s: abs(s.dte - TARGET_DTE)), not preferred


#: Legs each published posture is allowed to carry. V22 is the only variant
#: that pairs the put fly with a long ITM call; the others are all-put.
POSTURE_SHAPE = {
    "v14": {"calls": 0, "put_legs": 4},
    "v17": {"calls": 0, "put_legs": 4},
    "v22": {"calls": 1, "put_legs": 3},
}


def posture_check(strategy: str, card: dict, legs: list[Leg]) -> list[str]:
    """Structural invariants each translation must satisfy.

    A thin or stale chain can snap two strikes onto the same listed value, or
    invert an intended ordering, and quietly produce a structure that is no
    longer the posture it claims to be. These assert the built legs against
    the published shape — they make no claim about the market view, so they
    can be checked without inventing doctrine.
    """
    shape = POSTURE_SHAPE.get(strategy, {})
    flags = []
    puts = [leg for leg in legs if leg.cp == "P"]
    calls = [leg for leg in legs if leg.cp == "C"]
    if shape:
        if len(calls) != shape["calls"]:
            flags.append(f"expected {shape['calls']} call leg(s), built {len(calls)}")
        if len(puts) != shape["put_legs"]:
            flags.append(f"expected {shape['put_legs']} put leg(s), built {len(puts)}")
    put_strikes = [leg.strike for leg in puts]
    if len(set(put_strikes)) != len(put_strikes):
        flags.append("two put legs snapped to the same listed strike — widen "
                     "the structure or use a deeper chain")
    if sorted(put_strikes, reverse=True) != put_strikes:
        flags.append("put legs are not in descending strike order")
    if sum(leg.qty for leg in legs) <= 0:
        flags.append("net long-option count is not positive — the structure "
                     "is not defined-risk as built")
    if float(card.get("max_loss") or 0.0) >= 0:
        flags.append("model max loss is non-negative — check the leg ratios")
    return flags


def _leg(ctx: Context, slc, cp: str, strike: float, qty: int) -> Leg:
    from core.chain import iv_at

    k = ctx.snap(strike)
    return Leg(cp=cp, strike=k, expiry=slc.expiry, qty=qty, iv=iv_at(slc, k))


def _card(ctx: Context, key: str, name: str, structure: str,
          legs: list[Leg], score: float, rationale: list[str]) -> dict:
    metrics = struct_metrics(ctx.spot, legs, ctx.today, q=ctx.q)
    greeks = struct_greeks(ctx.spot, legs, ctx.today, q=ctx.q)
    suggestion = Suggestion(
        strategy=key,
        label=name,
        legs=legs,
        net_mid=metrics["entry"],
        greeks={k: round(v * MULT, 2) for k, v in greeks.items()},
        max_profit=metrics["max_profit"],
        max_loss=metrics["max_loss"],
        breakevens=metrics["breakevens"],
        score=score,
        rationale=rationale,
        cash_required=round(abs(metrics["max_loss"]) * MULT, 2),
        evidence={"hypothesis_id": f"X4-{key.upper()}", "status": "HYPOTHESIS"},
        policy_id=POLICY_ID,
        blocks=["X4 strike translation requires manual OptionNet/TWS review before execution"],
    )
    out = suggestion.to_dict()
    out.update(
        x4_structure=structure,
        manual_test_allowed=True,
        tws_stage_allowed=False,
        permitted=False,
        mid_src="model",
    )
    return out


def _auto_conditions(ctx: Context) -> dict:
    reg = ctx.regime
    atr_pct = 100 * float(reg.get("atr") or 0) / max(ctx.spot, 1)
    if reg.get("vol_state") == "STR" or (
            reg.get("gamma") == "-g" and atr_pct >= 1.1):
        setup = "whippy"
    elif reg.get("trend") == "RNG":
        setup = "range"
    elif reg.get("trend") == "UP" and int(reg.get("bias") or 0) >= 2:
        setup = "breakout"
    elif reg.get("trend") == "UP":
        setup = "support"
    else:
        setup = "whippy"

    iv_state = ("spike" if reg.get("vol_state") == "STR" or
                abs(float(reg.get("iv_chg_pct") or 0)) >= 8 else
                "elevated" if reg.get("vol_state") == "ELV" else "normal")
    bias = int(reg.get("bias") or 0)
    posture = "bullish" if bias > 0 else "vol" if bias < 0 else "allweather"
    return {"setup": setup, "iv_state": iv_state, "posture": posture,
            "atr_pct": round(atr_pct, 2)}


def _scores(ctx: Context, setup: str, iv_state: str, posture: str) -> dict:
    score = {"v14": 0.0, "v17": 0.0, "v22": 0.0}
    if setup == "support":
        score["v17"] += 4
    elif setup == "range":
        score["v22"] += 4
    elif setup == "whippy":
        score["v14"] += 5
    elif setup == "breakout":
        score["v17"] += 3

    if iv_state == "normal":
        score["v17"] += 2
    elif iv_state == "elevated":
        score["v22"] += 3
        score["v14"] += 2
    elif iv_state == "spike":
        score["v14"] += 5

    if posture == "bullish":
        score["v17"] += 4
    elif posture == "allweather":
        score["v14"] += 4
    elif posture == "vol":
        score["v22"] += 4

    reg, term = ctx.regime, ctx.regime.get("term", {})
    if reg.get("trend") == "UP":
        score["v17"] += 1
    if reg.get("trend") == "RNG":
        score["v22"] += 1
    if reg.get("gamma") == "-g":
        score["v14"] += 1
    if term.get("skew_rich"):
        score["v14"] += .5
        score["v17"] += .5
    return score


def _build_v14(ctx: Context, slc, em: float, score: float) -> dict:
    upper = ctx.snap(ctx.spot - .10 * em)
    body = ctx.snap(ctx.spot - .55 * em)
    near = max(upper - body, ctx.spot * .004)
    lower = ctx.snap(body - 2.0 * near)
    tail = ctx.snap(lower - max(.75 * near, ctx.spot * .008))
    legs = [_leg(ctx, slc, "P", upper, +1), _leg(ctx, slc, "P", body, -2),
            _leg(ctx, slc, "P", lower, +1), _leg(ctx, slc, "P", tail, +1)]
    return _card(ctx, "v14", f"V14 all-weather protected BWB · {slc.dte} DTE",
                 "Put BWB + separate long-tail put", legs, score, [
                     f"Listed strikes {upper:g}/-{body:g}x2/{lower:g} plus {tail:g}P tail hedge.",
                     "Neutral-defensive translation: broken lower wing plus explicit crash convexity.",
                     "Use when ATR/IV instability is the dominant problem; technical invalidation still governs the exit.",
                 ])


def _build_v17(ctx: Context, slc, em: float, score: float) -> dict:
    upper = ctx.snap(ctx.spot - .05 * em)
    body = ctx.snap(ctx.spot - .45 * em)
    near = max(upper - body, ctx.spot * .004)
    lower = ctx.snap(body - 2.25 * near)
    tail = ctx.snap(lower - max(.65 * near, ctx.spot * .007))
    legs = [_leg(ctx, slc, "P", upper, +1), _leg(ctx, slc, "P", body, -2),
            _leg(ctx, slc, "P", lower, +1), _leg(ctx, slc, "P", tail, +1)]
    card = _card(ctx, "v17", f"V17 bullish opportunistic BWB · {slc.dte} DTE",
                 "Bullish put BWB + separate long-tail put", legs, score, [
                     f"Listed strikes {upper:g}/-{body:g}x2/{lower:g} plus {tail:g}P tail hedge.",
                     "Upper expiration plateau should be at or above zero only when the executable package is a credit.",
                     "Anchor entry to confirmed support or breakout; reject the trade if live pricing leaves material upside loss.",
                 ])
    card["upside_plateau"] = round(-card["net_mid"] * MULT, 2)
    return card


def _build_v22(ctx: Context, slc, em: float, score: float) -> dict:
    body = ctx.snap(ctx.spot - .30 * em)
    width = max(.55 * em, ctx.spot * .006)
    high, low = ctx.snap(body + width), ctx.snap(body - width)
    call = (slc.put25_strike if slc.put25_strike else
            ctx.spot * math.exp(-.6745 * slc.atm_iv * math.sqrt(slc.dte / 365)))
    call = ctx.snap(call)
    one_fly = [_leg(ctx, slc, "P", high, +1), _leg(ctx, slc, "P", body, -2),
               _leg(ctx, slc, "P", low, +1)]
    one_call = _leg(ctx, slc, "C", call, +1)
    fly_delta = struct_greeks(ctx.spot, one_fly, ctx.today, q=ctx.q)["delta"]
    call_delta = struct_greeks(ctx.spot, [one_call], ctx.today, q=ctx.q)["delta"]
    # M3-style structures normally need multiple flies against one ITM call.
    # Solve the integer ratio from model delta instead of silently assuming 1:1.
    ratio = (max(1, min(12, round(-call_delta / fly_delta)))
             if fly_delta < -.005 else 1)
    legs = [_leg(ctx, slc, "P", high, +ratio),
            _leg(ctx, slc, "P", body, -2 * ratio),
            _leg(ctx, slc, "P", low, +ratio), one_call]
    return _card(ctx, "v22", f"V22 adaptive fly + long call · {slc.dte} DTE",
                 "Symmetrical put butterfly + approximately 75-delta ITM call",
                 legs, score, [
                     f"{ratio}x symmetric put fly {low:g}/{body:g}/{high:g} plus one long {call:g}C.",
                     "Body is placed with a modest bearish tint; the ITM call restores upside participation and delta.",
                     f"Model-delta ratio is {ratio}:1; wing width is IV-expected-move based. Rebuild only after a persistent, material skew change.",
                 ])


def build_x4(ctx: Context, *, setup: str = "auto", iv_state: str = "auto",
             posture: str = "auto") -> dict:
    """Return exact listed X4 legs plus the market evidence behind the choice."""
    auto = _auto_conditions(ctx)
    setup = auto["setup"] if setup == "auto" else setup
    iv_state = auto["iv_state"] if iv_state == "auto" else iv_state
    posture = auto["posture"] if posture == "auto" else posture
    if setup not in {"support", "range", "whippy", "breakout"}:
        raise ValueError("setup must be auto, support, range, whippy, or breakout")
    if iv_state not in {"normal", "elevated", "spike"}:
        raise ValueError("iv_state must be auto, normal, elevated, or spike")
    if posture not in {"bullish", "allweather", "vol"}:
        raise ValueError("posture must be auto, bullish, allweather, or vol")

    slc, dte_fell_back = _slice(ctx)
    if not slc:
        raise RuntimeError("no listed option expiry between 30 and 85 DTE")
    em = _iv_expected_move(ctx, slc.dte, slc.atm_iv)
    scores = _scores(ctx, setup, iv_state, posture)
    cards = [_build_v14(ctx, slc, em, scores["v14"]),
             _build_v17(ctx, slc, em, scores["v17"]),
             _build_v22(ctx, slc, em, scores["v22"])]
    card_legs = {c["strategy"]: [Leg(cp=leg["cp"], strike=leg["strike"],
                                     expiry=date.fromisoformat(leg["expiry"]),
                                     qty=leg["qty"], iv=leg["iv"])
                                 for leg in c["legs_raw"]] for c in cards}
    cards.sort(key=lambda c: (-c["score"], STRATEGY_ORDER.index(c["strategy"])))
    for rank, card in enumerate(cards, 1):
        card["rank"] = rank
        posture_flags = posture_check(card["strategy"], card,
                                      card_legs[card["strategy"]])
        card["posture_flags"] = posture_flags
        for flag in posture_flags:
            card.setdefault("blocks", []).append(f"POSTURE: {flag}")
        card["framework_alignment"] = round(100 * card["score"] /
                                             max(sum(scores.values()), 1), 0)

    term = ctx.regime.get("term", {})
    wait_reasons = []
    if term.get("verdict") == "INVERTED FRONT":
        wait_reasons.append("front term structure is inverted")
    if float(ctx.regime.get("vrp_fwd") or 0) <= -2:
        wait_reasons.append("forward VRP is materially negative")
    if not ctx.data.get("fresh", True):
        wait_reasons.append("market data is stale")
    if dte_fell_back:
        wait_reasons.append(
            f"no listed expiry in the {PREFERRED_DTE[0]}-{PREFERRED_DTE[1]} DTE "
            f"window; built at {slc.dte} DTE, which is not the published tenor")
    if any(c.get("posture_flags") for c in cards):
        wait_reasons.append("a structural posture check failed")
    action = ("WAIT — model only: " + "; ".join(wait_reasons)
              if wait_reasons else f"MODEL {cards[0]['strategy'].upper()} — manual validation required")
    return {
        "policy_id": POLICY_ID,
        "mode": ctx.mode,
        "symbol": ctx.symbol,
        "spot": round(ctx.spot, 2),
        "action": action,
        "recommended_strategy": cards[0]["strategy"],
        "conditions": {"setup": setup, "iv_state": iv_state, "posture": posture,
                       "auto": auto},
        "inputs": {
            "trend": ctx.regime.get("trend"), "bias": ctx.regime.get("bias"),
            "adx": ctx.regime.get("adx"), "atr": ctx.regime.get("atr"),
            "atr_pct": auto["atr_pct"], "iv30": ctx.regime.get("iv30"),
            "iv_percentile": ctx.regime.get("iv_pctl"),
            "iv_change_pct": ctx.regime.get("iv_chg_pct"),
            "rv21": ctx.regime.get("rv21"), "vrp_fwd": ctx.regime.get("vrp_fwd"),
            "gamma": ctx.regime.get("gamma"), "term": term.get("verdict"),
            "rr25": term.get("rr25_30d"), "skew_rich": term.get("skew_rich"),
            "expiry": slc.expiry.isoformat(), "dte": slc.dte,
            "dte_window": list(PREFERRED_DTE), "dte_fell_back": dte_fell_back,
            "atm_iv_expiry": round(slc.atm_iv * 100, 2),
            "expected_move": round(em, 2),
        },
        "data": ctx.data,
        "cards": cards,
        "notes": [
            "TWS supplies the current listed chain, option IV, 25-delta skew, NBBO and Greeks; daily price history supplies regime context.",
            "The source material does not specify a complete mechanical strike algorithm. These legs are a transparent engineering translation, not canonical Locke rules.",
            "No X4 card is enabled for order staging until matched-date and forward tests validate the translation.",
        ],
    }
