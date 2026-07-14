"""Candidate persistence and server-side staging validation."""
from __future__ import annotations

from datetime import datetime, time
from zoneinfo import ZoneInfo

from store.campaigns import CampaignStore, campaign_store

NY = ZoneInfo("America/New_York")


def within_execution_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(NY)
    if now.tzinfo is None:
        now = now.replace(tzinfo=NY)
    else:
        now = now.astimezone(NY)
    return now.weekday() < 5 and time(15, 0) <= now.time().replace(tzinfo=None) <= time(15, 40)


def persist_cards(payload: dict, store: CampaignStore | None = None,
                  ttl_seconds: int = 900) -> dict:
    store = store or campaign_store()
    context = {"session": payload.get("data", {}).get("session"),
               "fresh": payload.get("data", {}).get("fresh", False),
               "spot": payload.get("spot"), "regime": payload.get("inputs", {}),
               "action": payload.get("action"),
               "as_of_time": payload.get("data", {}).get("as_of_time"),
               "historical": payload.get("data", {}).get("historical", False),
               "test_session_id": payload.get("test_session_id")}
    for card in payload.get("cards", []):
        cid = store.save_candidate(payload["symbol"],
                                   (payload.get("account") or {}).get("account"),
                                   payload.get("mode", "mock"),
                                   payload.get("policy_id", "unknown"), context, card,
                                   ttl_seconds=ttl_seconds)
        card["candidate_id"] = cid
    return payload


def validate_for_stage(candidate_id: str, requested_qty: int,
                       store: CampaignStore | None = None) -> dict:
    store = store or campaign_store()
    cand = store.candidate(candidate_id, require_fresh=True)
    if not cand:
        raise ValueError("candidate missing or expired; rescan")
    card, ctx = cand["card"], cand["context"]
    # Mock staging is an inert preview for OptionNet/manual testing. Evidence
    # and live-risk blocks remain visible on the card but do not prevent that
    # preview; they are enforced for every non-mock staging request.
    blocks = [] if cand["mode"] == "mock" else list(card.get("blocks") or [])
    allowed = int(card.get("governor", {}).get("approved_lots") or 0)
    qty = int(requested_qty)
    if not ctx.get("fresh"):
        blocks.append("market data is stale")
    if qty < 1:
        blocks.append("quantity must be positive")
    if qty > (max(allowed, 1) if cand["mode"] == "mock" else allowed):
        blocks.append(f"quantity {qty} exceeds governor approval {allowed}")
    if not card.get("tws_stage_allowed", False) and cand["mode"] != "mock":
        blocks.append("strategy evidence status does not permit TWS staging")
    if cand["mode"] != "mock" and not within_execution_window():
        blocks.append("TWS staging is allowed only 15:00-15:40 ET")
    if blocks:
        raise ValueError("; ".join(dict.fromkeys(blocks)))
    return {"candidate": cand, "card": card, "quantity": qty}
