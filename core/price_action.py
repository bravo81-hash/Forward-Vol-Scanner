"""Fail-open Price-Action research context for the stock radar.

This module never grants execution permission and never changes the V1 ranking.
It records a small shadow adjustment so confirmation value can be measured before
any future promotion decision.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from urllib.request import Request, urlopen

DEFAULT_FEED_URL = (
    "https://raw.githubusercontent.com/bravo81-hash/Price-Action/main/"
    "docs/data/fvs_feed.json"
)
MAX_BYTES = 1_000_000
MAX_AGE_HOURS = 96


def _timestamp(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def validate_feed(payload: object, *, now: datetime | None = None) -> dict:
    """Validate the v1 trust boundary and return display-safe feed metadata."""
    if not isinstance(payload, dict):
        raise ValueError("Price-Action feed must be a JSON object")
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported Price-Action feed schema")
    if payload.get("market") != "us" or payload.get("authority") != "context_only":
        raise ValueError("Price-Action feed is not US context-only data")
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise ValueError("Price-Action feed rows are missing")
    generated = _timestamp(payload.get("generated"))
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_hours = (current - generated).total_seconds() / 3600
    if age_hours < -1:
        raise ValueError("Price-Action feed timestamp is in the future")
    status = "FRESH" if age_hours <= MAX_AGE_HOURS else "STALE"
    return {
        "status": status,
        "schema_version": 1,
        "generated": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "age_hours": round(max(age_hours, 0), 1),
        "row_count": len(rows),
        "authority": "SHADOW_ONLY",
        "source": payload.get("source") or "Price-Action",
    }


def fetch_feed(*, url: str | None = None, now: datetime | None = None,
               timeout: float = 4.0) -> tuple[dict | None, dict]:
    """Fetch and validate the feed. Every failure is returned, never raised."""
    target = url if url is not None else os.getenv("FVS_PRICE_ACTION_FEED", DEFAULT_FEED_URL)
    if str(target).strip().lower() in {"", "off", "none", "disabled"}:
        return None, {"status": "DISABLED", "authority": "SHADOW_ONLY",
                      "matched_count": 0}
    try:
        request = Request(str(target), headers={"User-Agent": "Forward-Vol-Scanner/1"})
        with urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Price-Action feed exceeds size limit")
        payload = json.loads(raw.decode("utf-8"))
        meta = validate_feed(payload, now=now)
        if meta["status"] != "FRESH":
            return None, {**meta, "matched_count": 0}
        return payload, {**meta, "matched_count": 0}
    except Exception as exc:  # noqa: BLE001 - optional context must fail open
        return None, {"status": "UNAVAILABLE", "authority": "SHADOW_ONLY",
                      "matched_count": 0, "reason": str(exc)[:180]}


def practice_feed(ideas: list[dict], *, now: datetime | None = None) -> tuple[dict, dict]:
    """Deterministic context badges for the Codespaces/practice workflow."""
    rows = []
    for i, idea in enumerate(ideas[:6]):
        direction = idea.get("direction")
        side = "long" if direction == "BULL" else "short"
        signal = ("S1", "S2", "S3", "S4")[i % 4]
        if i == 1:
            side = "short" if side == "long" else "long"
        if signal == "S3":
            side = "neutral"
        rows.append({"ticker": idea.get("symbol"), "signal": signal, "side": side,
                     "score": round(0.8 - i * .03, 2), "rank": i + 1,
                     "evidence_tier": {"S1": "CONTEXT", "S2": "CAUTION",
                                       "S3": "PREFERRED", "S4": "EXPERIMENTAL"}[signal],
                     "evidence_reason": "practice-only integration context",
                     "regime": "practice", "align": "practice"})
    generated = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    payload = {"schema_version": 1, "source": "Price-Action practice",
               "market": "us", "authority": "context_only",
               "generated": generated.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "bench": {"symbol": "SPY", "bias": "neutral",
                         "state": "PRACTICE", "guidance": "practice data"},
               "rows": rows}
    meta = validate_feed(payload, now=generated)
    return payload, {**meta, "status": "PRACTICE", "matched_count": 0}


def enrich_ideas(ideas: list[dict], payload: dict | None, meta: dict) -> tuple[list[dict], dict]:
    """Attach shadow annotations without modifying score, rank or permissions."""
    by_symbol = {}
    if payload:
        for row in payload.get("rows", []):
            if isinstance(row, dict) and row.get("ticker"):
                by_symbol[str(row["ticker"]).upper()] = row
    enriched, matched = [], 0
    counts = {"CONFIRM": 0, "CONFLICT": 0, "NEUTRAL_RESEARCH": 0,
              "EXPERIMENTAL": 0}
    for idea in ideas:
        row = by_symbol.get(str(idea.get("symbol") or "").upper())
        if not row:
            context = {"matched": False, "status": "NO_SIGNAL", "adjustment": 0,
                       "authority": "SHADOW_ONLY"}
        else:
            matched += 1
            signal = str(row.get("signal") or "").upper()
            side = str(row.get("side") or "").lower()
            expected = "long" if idea.get("direction") == "BULL" else "short"
            if signal == "S3" or side == "neutral":
                status, adjustment = "NEUTRAL_RESEARCH", 0
            elif signal == "S4":
                status, adjustment = "EXPERIMENTAL", 0
            elif side == expected:
                status, adjustment = "CONFIRM", 2
            else:
                status, adjustment = "CONFLICT", -2
            counts[status] += 1
            context = {
                "matched": True, "status": status, "adjustment": adjustment,
                "shadow_score": round(float(idea.get("score") or 0) + adjustment, 1),
                "signal": signal, "side": side, "evidence_tier": row.get("evidence_tier"),
                "evidence_reason": row.get("evidence_reason"), "rank": row.get("rank"),
                "score": row.get("score"), "regime": row.get("regime"),
                "align": row.get("align"), "authority": "SHADOW_ONLY",
            }
        enriched.append({**idea, "price_action": context})
    out_meta = {**meta, "authority": "SHADOW_ONLY", "matched_count": matched,
                "match_counts": {k: v for k, v in counts.items() if v},
                "bench": (payload or {}).get("bench") or {}}
    return enriched, out_meta
