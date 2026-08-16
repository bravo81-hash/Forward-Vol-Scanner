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
    # One indexed join, not a per-campaign fan-out: the previous version read
    # every campaign (with its events, tests and orders) and decoded every
    # card JSON in Python to produce these eight columns.
    rows, sessions = [], defaultdict(list)
    for row in store.manual_test_rows():
        row["test_mode"] = row.get("test_mode") or "optionnet"
        rows.append(row)
        if row.get("session_id"):
            sessions[(row["test_mode"], row["session_id"])].append(row)
    matched = [{"session_id": sid, "test_mode": mode,
                "strategies_tested": len({r["strategy"] for r in rs}),
                "results_recorded": len(rs),
                "complete_comparison": len({r["strategy"] for r in rs}) >= 2}
               for (mode, sid), rs in sorted(sessions.items())]
    historical = [x for x in matched if x["test_mode"] != "optionnet_forward_live"]
    forward = [x for x in matched if x["test_mode"] == "optionnet_forward_live"]
    return {"hypotheses": hypotheses, "manual_results": by_strategy,
            "observations": rows, "matched_sessions": matched,
            "complete_matched_sessions": sum(int(x["complete_comparison"]) for x in matched),
            "complete_historical_sessions": sum(int(x["complete_comparison"])
                                                for x in historical),
            "complete_forward_sessions": sum(int(x["complete_comparison"])
                                             for x in forward),
            "limitation": "OptionNet/manual and paper observations are evidence, not a full historical-chain backtest.",
            "automatic_promotion": False}
