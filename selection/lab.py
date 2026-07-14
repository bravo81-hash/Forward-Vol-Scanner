"""Strategy laboratory: construct every permitted family for manual ONE tests."""
from __future__ import annotations

from config.loader import account_profile, hypothesis
from portfolio.governor import evaluate_candidate
from selection.manage import management_plan
from strategies import REGISTRY

MULTI = {"calendar", "double_calendar", "diagonal"}


def strategy_lab(ctx, intent: str, account: str | None, nlv: float | None) -> dict:
    profile = account_profile(account, nlv)
    bias = {"bull": 1, "bear": -1, "neutral": 0}.get(intent)
    if bias is None:
        bias = 1 if ctx.regime.get("bias", 0) > 0 else -1 if ctx.regime.get("bias", 0) < 0 else 0
    cards = []
    for key, strat in REGISTRY.items():
        if profile.get("block_multi_expiry") and key in MULTI:
            continue
        if key == "target_fly":
            # Neutral lab still exposes both directional variants.
            props = (strat.propose_for_bias(ctx, bias) if bias else
                     strat.propose_for_bias(ctx, 1)[:1] + strat.propose_for_bias(ctx, -1)[:1])
        else:
            props = strat.propose(ctx)
        for s in props[:2]:
            s.manage = management_plan(ctx, s)
            hyp_id = s.evidence.get("hypothesis_id")
            ev = hypothesis(hyp_id) if hyp_id else {"status": "ACTIVE", "name": "existing engine"}
            s.evidence = {"hypothesis_id": hyp_id, "status": ev.get("status", "ACTIVE"),
                          "name": ev.get("name")}
            card = s.to_dict()
            card["selection_source"] = "strategy laboratory"
            card["manual_test_allowed"] = True
            card["tws_stage_allowed"] = ctx.mode == "mock"
            card["governor"] = evaluate_candidate(card, ctx.book, profile["nlv"], ctx.spot, "FULL")
            card["lots"] = {"lots": card["governor"]["approved_lots"],
                            "binding": card["governor"]["binding"], "size": "FULL"}
            cards.append(card)
    cards.sort(key=lambda c: (c["strategy"], -c["score"]))
    return {"symbol": ctx.symbol, "spot": ctx.spot, "mode": ctx.mode,
            "action": "STRATEGY LAB — MANUAL OPTIONNET TESTING",
            "intent": intent, "bias": bias, "policy_id": "strategy-lab-v3",
            "account": profile, "data": ctx.data, "book": ctx.book,
            "inputs": {"iv30": ctx.regime.get("iv30"),
                       "iv_band": ctx.regime.get("vol_state"),
                       "iv_band_src": "current context",
                       "rr25_30d": ctx.regime.get("term", {}).get("rr25_30d", 0),
                       "term": ctx.regime.get("term", {}).get("verdict"),
                       "vrp_fwd": ctx.regime.get("vrp_fwd"),
                       "har_rv": ctx.regime.get("har_rv")},
            "cards": cards,
            "notes": ["Laboratory shows all account-permitted families regardless of current rank.",
                      "Hypothesis cards are for OptionNet/paper evidence, not validated live rules."]}
