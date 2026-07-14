"""Campaign Engine v3 selector: current engine plus chat-derived test rows."""
from __future__ import annotations

from portfolio.governor import evaluate_candidate
from selection.ranker import shortlist as legacy_shortlist
from selection.smsf import smsf_shortlist


def campaign_shortlist(ctx, intent: str, account: str | None, nlv: float | None) -> dict:
    gate = smsf_shortlist(ctx, intent, account, nlv)
    if (ctx.mandate or {}).get("block_multi_expiry"):
        for card in gate["cards"]:
            card["selection_source"] = "cash-account Gate S"
        return gate

    legacy = legacy_shortlist(ctx)
    seen, cards = set(), []
    for card in legacy.get("cards", []):
        sig = tuple((leg["cp"], leg["strike"], leg["expiry"], leg["qty"])
                    for leg in card.get("legs_raw", []))
        seen.add(sig)
        card["selection_source"] = "existing TE engine"
        card["evidence"] = {"hypothesis_id": None, "status": "ACTIVE",
                            "name": "existing strategy engine"}
        card["policy_id"] = "te-ranker-v2"
        card.setdefault("cash_required", abs(float(card.get("max_loss") or 0)) * 100)
        card.setdefault("blocks", [])
        gov = evaluate_candidate(card, ctx.book, nlv, ctx.spot, legacy.get("size", "FULL"))
        card["governor"] = gov
        card["lots"] = {"lots": gov["approved_lots"], "binding": gov["binding"],
                        "size": gov["size"]}
        card["manual_test_allowed"] = True
        card["tws_stage_allowed"] = ctx.mode in ("mock", "live")
        cards.append(card)

    for card in gate["cards"]:
        sig = tuple((leg["cp"], leg["strike"], leg["expiry"], leg["qty"])
                    for leg in card.get("legs_raw", []))
        if sig in seen:
            continue
        card["selection_source"] = "chat-derived Gate S test row"
        cards.append(card)
        seen.add(sig)

    gate["cards"] = cards[:10]
    gate["action"] = f"ACTION: REVIEW {legacy.get('verdict', 'current engine')} + GATE S TEST ROWS"
    gate["legacy_verdict"] = legacy.get("verdict")
    gate["policy_id"] = "campaign-selector-v3"
    return gate
