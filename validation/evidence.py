"""Evidence summaries; intentionally separates manual results from proof."""
from __future__ import annotations

from collections import defaultdict

from config.loader import hypothesis_config
from store.campaigns import CampaignStore, campaign_store


def evidence_report(store: CampaignStore | None = None) -> dict:
    store = store or campaign_store()
    by_strategy = store.evidence_summary()
    hypotheses = [{"id": h.get("id"), "name": h.get("name"),
                   "status": h.get("status", "HYPOTHESIS")}
                  for h in hypothesis_config().get("hypotheses", [])]
    rows, sessions = [], defaultdict(list)
    for campaign in store.campaigns(limit=5000):
        card = campaign.get("card", {})
        sid = card.get("test_session_id")
        for test in campaign.get("manual_tests", []):
            row = {"session_id": sid, "strategy": campaign.get("strategy"),
                   "market_state": card.get("market_state"), "rank": card.get("rank"),
                   "result_pct": test.get("result_pct"),
                   "max_drawdown_pct": test.get("max_drawdown_pct"),
                   "setup_rating": test.get("setup_rating")}
            rows.append(row)
            if sid:
                sessions[sid].append(row)
    matched = [{"session_id": sid, "strategies_tested": len({r["strategy"] for r in rs}),
                "results_recorded": len(rs),
                "complete_comparison": len({r["strategy"] for r in rs}) >= 2}
               for sid, rs in sorted(sessions.items())]
    return {"hypotheses": hypotheses, "manual_results": by_strategy,
            "observations": rows, "matched_sessions": matched,
            "complete_matched_sessions": sum(int(x["complete_comparison"]) for x in matched),
            "limitation": "OptionNet/manual and paper observations are evidence, not a full historical-chain backtest.",
            "automatic_promotion": False}
