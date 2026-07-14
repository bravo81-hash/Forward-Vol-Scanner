"""Observed-decision replay summary from captured point-in-time snapshots."""
from __future__ import annotations

from collections import Counter

from store.campaigns import CampaignStore, campaign_store


def replay_summary(store: CampaignStore | None = None, symbol: str | None = None) -> dict:
    store = store or campaign_store()
    rows = store.snapshots(symbol, limit=2000)
    actions, regimes, sources = Counter(), Counter(), Counter()
    stale = 0
    for row in rows:
        payload = row["payload"]
        actions[payload.get("action", "UNKNOWN")] += 1
        reg = payload.get("regime", {})
        regimes[f"{reg.get('vol_state','?')}·{reg.get('trend','?')}"] += 1
        sources[row["source"]] += 1
        stale += int(not row["fresh"])
    return {"snapshots": len(rows), "symbol": symbol,
            "actions": dict(actions), "regime_cells": dict(regimes),
            "sources": dict(sources), "stale": stale,
            "limitation": "This is an observed context replay. Position outcomes require paper or manual OptionNet evidence."}


if __name__ == "__main__":  # pragma: no cover
    import json
    print(json.dumps(replay_summary(), indent=2))
