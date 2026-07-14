"""Evidence summaries; intentionally separates manual results from proof."""
from __future__ import annotations

from config.loader import hypothesis_config
from store.campaigns import CampaignStore, campaign_store


def evidence_report(store: CampaignStore | None = None) -> dict:
    store = store or campaign_store()
    by_strategy = store.evidence_summary()
    hypotheses = [{"id": h.get("id"), "name": h.get("name"),
                   "status": h.get("status", "HYPOTHESIS")}
                  for h in hypothesis_config().get("hypotheses", [])]
    return {"hypotheses": hypotheses, "manual_results": by_strategy,
            "limitation": "OptionNet/manual and paper observations are evidence, not a full historical-chain backtest.",
            "automatic_promotion": False}
