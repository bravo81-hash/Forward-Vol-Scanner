"""Match broker positions to campaign legs without silent guessing."""
from __future__ import annotations


def _key(symbol: str, leg: dict) -> tuple:
    return (symbol.upper(), str(leg.get("expiry", ""))[:10],
            float(leg.get("strike", 0)), str(leg.get("cp", "")).upper())


def reconcile_positions(campaigns: list[dict], positions: list[dict]) -> dict:
    owners: dict[tuple, list[tuple[str, int]]] = {}
    for campaign in campaigns:
        qty = int(campaign.get("quantity") or 1)
        for leg in campaign.get("card", {}).get("legs_raw", []):
            owners.setdefault(_key(campaign["symbol"], leg), []).append(
                (campaign["id"], int(leg["qty"]) * qty))

    exact, partial, ambiguous, unassigned = [], [], [], []
    for pos in positions:
        matches = owners.get(_key(pos.get("symbol", ""), pos), [])
        row = {"position": pos, "matches": [{"campaign_id": c, "expected_qty": q}
                                               for c, q in matches]}
        if not matches:
            unassigned.append(row)
        elif len(matches) > 1:
            ambiguous.append(row)
        elif int(pos.get("qty", 0)) == matches[0][1]:
            exact.append(row)
        else:
            partial.append(row)
    return {"exact": exact, "partial": partial, "ambiguous": ambiguous,
            "unassigned": unassigned,
            "complete": not (partial or ambiguous or unassigned)}
