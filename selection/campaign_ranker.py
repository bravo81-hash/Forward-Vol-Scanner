"""One transparent, conditional ranker for every Campaign Engine family.

The rank is a test hypothesis, not proof of edge.  It combines the frozen
market-state matrix with each factory's structure-quality score, applies
account and audit blocks first, and returns the same ordering for the normal
shortlist and the expanded laboratory.
"""
from __future__ import annotations

import hashlib
import json

from config.loader import account_profile, hypothesis
from portfolio.governor import evaluate_candidate
from selection.manage import management_plan
from strategies import REGISTRY

MULTI_EXPIRY = {"calendar", "double_calendar", "diagonal"}
INCOME_OR_SHORT_FRONT = {
    "calendar", "double_calendar", "diagonal", "condor", "bwb", "butterfly",
    "balanced_fly", "iron_fly", "otm_put_fly", "call_bwb", "m3_bwb_call",
}


def _bias(ctx, intent: str) -> int:
    explicit = {"bull": 1, "neutral": 0, "bear": -1}.get(intent)
    if explicit is not None:
        return explicit
    return 1 if ctx.regime.get("bias", 0) > 0 else -1 if ctx.regime.get("bias", 0) < 0 else 0


def market_state(ctx) -> dict:
    reg, term = ctx.regime, ctx.regime.get("term", {})
    band = reg.get("vol_state", "NRM")
    vrp = float(reg.get("vrp_fwd") or 0.0)
    inverted = term.get("verdict") == "INVERTED FRONT"
    if inverted and band == "STR":
        return {"name": "CRISIS", "size": "QUARTER", "allow_carry": False,
                "reason": "Inverted front plus stressed IV: no new short-gamma or short-front exposure."}
    if inverted or vrp <= -2.0:
        why = ("Inverted front: audit requires zero new carry and immediate de-gross review."
               if inverted else f"Forward VRP {vrp:+.1f}v: carry is unpaid; use debit participation or cash.")
        return {"name": "DEFENSIVE", "size": "HALF", "allow_carry": False, "reason": why}
    if band in ("ELV", "STR") or vrp < 3.0 or ctx.hard_gated():
        return {"name": "REDUCED_CARRY", "size": "HALF", "allow_carry": True,
                "reason": "Edge is conditional or tape risk is elevated; compare at reduced size."}
    return {"name": "CARRY", "size": "FULL", "allow_carry": True,
            "reason": "Positive forward VRP with no audit de-gross trigger."}


def _policy(key: str, ctx, bias: int, profile: dict, state: dict) -> dict:
    reg, term = ctx.regime, ctx.regime.get("term", {})
    band = reg.get("vol_state", "NRM")
    rr = float(term.get("rr25_30d") or 0.0)
    steep = bool(term.get("skew_rich"))
    vrp = float(reg.get("vrp_fwd") or 0.0)
    trend = reg.get("trend", "RNG")
    flat = not steep and rr > -0.5

    if profile.get("block_multi_expiry") and key in MULTI_EXPIRY:
        return {"permitted": False, "score": 0,
                "reason": "Blocked for this cash/SMSF account: multi-expiry position."}
    if not state["allow_carry"] and key in INCOME_OR_SHORT_FRONT:
        return {"permitted": False, "score": 0,
                "reason": f"Audit block in {state['name']}: no new carry or short-front exposure."}
    if key in {"target_fly", "debit_spread"} and bias == 0:
        return {"permitted": False, "score": 0,
                "reason": "Requires a pre-declared bullish or bearish thesis; neutral is stand aside."}

    score, reason = 30.0, "Permitted for research, but the selected state supplies no specific edge."
    if key == "calendar":
        if band == "CMP" and vrp < 3 and term.get("verdict") in ("CONTANGO", "STEEP CONTANGO", "FLAT"):
            score, reason = 84, "Cheap back vega and weak carry payment favour a debit calendar."
    elif key == "double_calendar":
        if band == "CMP" and bias == 0 and trend == "RNG":
            score, reason = 86, "Compressed IV plus a range supports a wider two-tent long-vega test."
        elif band == "CMP":
            score, reason = 68, "Low-IV long-vega comparator; directional drift can threaten one tent."
    elif key == "diagonal":
        if band in ("CMP", "NRM") and bias and trend != "RNG" and vrp < 3:
            score, reason = 88, "Low-paid carry plus directional trend favours a debit diagonal."
        elif bias:
            score, reason = 62, "Directional long-vega comparator, but the volatility state is not ideal."
    elif key == "condor":
        if band in ("NRM", "ELV") and bias == 0 and trend == "RNG" and vrp >= 3:
            score, reason = 85, "Range plus paid forward VRP supports a defined-risk condor."
        elif vrp > 0:
            score, reason = 50, "Premium is paid, but direction or tape state weakens the range thesis."
    elif key == "bwb":
        if band in ("NRM", "ELV") and steep and vrp >= 0:
            score, reason = 91, "Rich put skew subsidises the broken lower wing; primary carry hypothesis."
        elif steep:
            score, reason = 68, "Skew helps entry, but the broader carry state is weak."
    elif key == "butterfly":
        if ctx.events.get("opex_week") and trend == "RNG" and reg.get("gamma") == "+g":
            score, reason = 90, "OpEx range and positive dealer-gamma proxy support a pin-fly test."
        else:
            score, reason = 35, "Legacy generic fly has been superseded by explicit balanced, OTM and target rows."
    elif key == "balanced_fly":
        if flat and bias == 0 and band in ("NRM", "ELV", "STR"):
            score, reason = 89, "Flat skew and neutral bias favour the cash-efficient all-put balanced fly."
        elif flat:
            score, reason = 72, "Cash-efficient flat-skew comparator; directional fit is weaker."
        else:
            score, reason = 56, "Useful matched-date control against skew-dependent alternatives."
    elif key == "iron_fly":
        if band in ("ELV", "STR") and flat and bias == 0:
            score, reason = 88, "High IV and flat skew make the ATM straddle the paid exposure."
            if profile.get("cash_account"):
                score -= 10
                reason += " Cash penalty applied: compare against the all-put balanced fly."
        else:
            score, reason = 38, "No high-IV/flat-skew edge; retain only as a research control."
    elif key == "otm_put_fly":
        if band in ("ELV", "STR") and steep:
            score, reason = 93, "High IV plus steep put skew favours a wide downside tent at smaller size."
        elif bias < 0 and band == "NRM":
            score, reason = 79, "Bearish drift supplies the thesis; compare with the put BWB."
    elif key == "call_bwb":
        if rr <= -0.5:
            score, reason = 92, "Call skew is bid; the upside wing is the expensive side to sell."
        elif bias > 0:
            score, reason = 60, "Useful upside-repair comparator, but call skew does not fund a core entry."
    elif key == "m3_bwb_call":
        if bias > 0 and band == "NRM" and steep:
            score, reason = 94, "Bullish bias plus rich put skew fits the cash-account BWB + ITM-call substitute."
        elif bias > 0:
            score, reason = 70, "Bullish same-expiry comparator; skew subsidy is weaker."
    elif key == "target_fly":
        if state["name"] in ("DEFENSIVE", "CRISIS") or band == "CMP":
            score, reason = 82, "Debit-only convexity to a declared target avoids opening a carry campaign."
        else:
            score, reason = 58, "Directional target test; separate it from income-strategy comparisons."
    elif key == "debit_spread":
        if state["name"] in ("DEFENSIVE", "CRISIS"):
            score, reason = 94, "Primary defensive row: defined debit participation with no short tail."
        elif band == "CMP" or vrp < 0:
            score, reason = 84, "Cheap or unpaid carry favours directional debit over premium selling."
        else:
            score, reason = 60, "Directional comparator and debit-only repair candidate."
    return {"permitted": True, "score": float(score), "reason": reason}


def _tier(score: float) -> str:
    if score >= 85:
        return "PRIMARY"
    if score >= 70:
        return "COMPARATOR"
    if score >= 50:
        return "RESEARCH"
    return "NO EDGE"


def _session_id(ctx, profile: dict, inputs: dict) -> str:
    material = {"symbol": ctx.symbol, "account": profile.get("account"),
                "date": ctx.today.isoformat(), "time": ctx.data.get("as_of_time"),
                "inputs": inputs}
    return "ONE-" + hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()[:10].upper()


def rank_campaign(ctx, intent: str, account: str | None, nlv: float | None,
                  include_all: bool = False) -> dict:
    profile = dict(ctx.mandate or account_profile(account, nlv))
    profile.update(account=account, nlv=float(nlv or profile.get("nlv") or 100_000))
    bias = _bias(ctx, intent)
    state = market_state(ctx)
    cards, excluded = [], []
    for key, strat in REGISTRY.items():
        policy = _policy(key, ctx, bias, profile, state)
        if not policy["permitted"]:
            excluded.append({"strategy": key, "reason": policy["reason"], "status": "INELIGIBLE"})
            continue
        if key in {"target_fly", "debit_spread"}:
            props = strat.propose_for_bias(ctx, bias)
        else:
            props = strat.propose(ctx)
        if not props:
            excluded.append({"strategy": key, "reason": "No constructible expiry/variant for this date.",
                             "status": "UNAVAILABLE"})
            continue
        for suggestion in props[:2]:
            suggestion.manage = management_plan(ctx, suggestion)
            hyp_id = suggestion.evidence.get("hypothesis_id")
            ev = hypothesis(hyp_id) if hyp_id else {"status": "LEGACY", "name": "legacy TE family"}
            suggestion.evidence = {"hypothesis_id": hyp_id,
                                   "status": ev.get("status", "HYPOTHESIS"),
                                   "name": ev.get("name")}
            card = suggestion.to_dict()
            quality = max(-5.0, min(5.0, float(card.get("score") or 0.0)))
            # Family fit must dominate: legacy factories use incomparable
            # internal score scales (credit/width, curve edge, reward/debit).
            # Their bounded quality score only breaks ties inside a policy row.
            rank_score = round(policy["score"] + quality * .25, 2)
            card.update(selection_source="unified conditional ranker",
                        family_edge_score=policy["score"], rank_score=rank_score,
                        edge_tier=_tier(policy["score"]), rank_reason=policy["reason"],
                        market_state=state["name"], permitted=True,
                        manual_test_allowed=True)
            card["tws_stage_allowed"] = (ctx.mode == "mock" or
                                          ev.get("status") in ("PAPER", "ACTIVE"))
            if ctx.mode != "mock" and not card["tws_stage_allowed"]:
                card["blocks"].append("Hypothesis strategy: OptionNet/paper evidence required")
            gov = evaluate_candidate(card, ctx.book, profile["nlv"], ctx.spot, state["size"])
            card["governor"] = gov
            card["lots"] = {"lots": gov["approved_lots"], "binding": gov["binding"],
                            "size": gov["size"]}
            cards.append(card)

    cards.sort(key=lambda c: (c["rank_score"], c["score"]), reverse=True)
    if not include_all:
        # One best build per family makes matched-date testing comprehensible.
        seen, compact = set(), []
        for card in cards:
            if card["strategy"] in seen:
                continue
            compact.append(card)
            seen.add(card["strategy"])
            if len(compact) == 6:
                break
        cards = compact

    inputs = {"iv30": ctx.regime.get("iv30"), "iv_band": ctx.regime.get("vol_state"),
              "iv_band_src": "manual ONE snapshot" if ctx.data.get("historical") else "current context",
              "rv21": ctx.regime.get("rv21"), "vrp_fwd": ctx.regime.get("vrp_fwd"),
              "rr25_30d": ctx.regime.get("term", {}).get("rr25_30d", 0.0),
              "term": ctx.regime.get("term", {}).get("verdict"),
              "trend": ctx.regime.get("trend"), "bias": bias,
              "event": "manual" if ctx.data.get("historical") else "current"}
    sid = _session_id(ctx, profile, inputs)
    for rank, card in enumerate(cards, 1):
        card["rank"] = rank
        card["test_session_id"] = sid
        card["one_recipe"] = {
            "entry_date": ctx.today.isoformat(), "entry_time_et": ctx.data.get("as_of_time", "15:30"),
            "spot": ctx.spot, "instruction": "In ONE choose this date/time, then use the nearest listed expiry and strikes to the model targets.",
        }

    action = (f"{state['name']}: {cards[0]['strategy'].replace('_', ' ').upper()} RANKS FIRST"
              if cards else f"{state['name']}: STAND ASIDE — no eligible strategy for this thesis")
    return {"symbol": ctx.symbol, "spot": ctx.spot, "mode": ctx.mode,
            "action": action, "intent": intent, "bias": bias,
            "policy_id": "campaign-ranker-v4", "market_state": state,
            "account": profile, "data": ctx.data, "book": ctx.book,
            "inputs": inputs, "test_session_id": sid, "cards": cards,
            "excluded": excluded,
            "notes": [state["reason"],
                      "Ranks are frozen expert hypotheses, not evidence of edge.",
                      "Compare strategies on the same ONE date/time and expiry target; do not choose dates after seeing outcomes."]}
