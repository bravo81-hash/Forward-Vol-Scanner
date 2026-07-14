import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from store.campaigns import CampaignStore
from execution.candidates import persist_cards, validate_for_stage
from execution.candidates import within_execution_window


def _payload():
    return {"symbol": "SPX", "spot": 6000, "mode": "mock", "policy_id": "test",
            "data": {"session": "2026-07-14", "fresh": True},
            "inputs": {}, "action": "TEST", "account": {"account": "MOCK-B"},
            "cards": [{"strategy": "balanced_fly", "label": "test fly",
                       "legs_raw": [{"cp": "P", "strike": 6000, "expiry": "2026-08-21", "qty": -2}],
                       "legs": ["-2 Aug21 6000P"], "net_mid": 1.0,
                       "max_loss": -10.0, "cash_required": 1000,
                       "greeks": {"delta": 0, "gamma": 0, "theta": 10, "vega": -20},
                       "evidence": {"status": "HYPOTHESIS"}, "blocks": [],
                       "tws_stage_allowed": True,
                       "governor": {"approved_lots": 2, "size": "FULL"}}]}


def test_candidate_campaign_lifecycle_and_manual_test(tmp_path):
    store = CampaignStore(tmp_path / "v3.sqlite")
    payload = persist_cards(_payload(), store)
    cid = payload["cards"][0]["candidate_id"]
    checked = validate_for_stage(cid, 1, store)
    assert checked["card"]["label"] == "test fly"
    campaign = store.create_campaign(cid, 1, "optionnet")
    assert campaign["state"] == "ELIGIBLE"
    campaign = store.transition(campaign["id"], "OPEN", payload={"paper": True})
    assert campaign["opened_at"]
    campaign = store.add_manual_test(campaign["id"], {"source": "OptionNet Explorer",
                                                      "setup_rating": 4, "result_pct": 11.0,
                                                      "max_drawdown_pct": -4.0, "notes": "good shape"})
    assert campaign["manual_tests"][0]["result_pct"] == 11.0
    assert store.evidence_summary()[0]["tests"] == 1


def test_matched_session_context_survives_campaign_creation(tmp_path):
    store = CampaignStore(tmp_path / "matched.sqlite")
    payload = _payload()
    payload["test_session_id"] = "ONE-ABC123"
    payload["cards"][0]["test_session_id"] = "ONE-ABC123"
    cid = persist_cards(payload, store)["cards"][0]["candidate_id"]
    campaign = store.create_campaign(cid, test_mode="optionnet_matched_date")
    assert campaign["card"]["test_session_id"] == "ONE-ABC123"
    assert store.candidate(cid)["context"]["test_session_id"] == "ONE-ABC123"


def test_candidate_quantity_and_expiry_enforced(tmp_path):
    store = CampaignStore(tmp_path / "v3.sqlite")
    payload = persist_cards(_payload(), store, ttl_seconds=1)
    cid = payload["cards"][0]["candidate_id"]
    with pytest.raises(ValueError):
        validate_for_stage(cid, 3, store)
    with store.connect() as c:
        c.execute("UPDATE candidates SET expires_at=? WHERE id=?", (time.time() - 1, cid))
    with pytest.raises(ValueError):
        validate_for_stage(cid, 1, store)


def test_terminal_campaign_cannot_reopen(tmp_path):
    store = CampaignStore(tmp_path / "v3.sqlite")
    cid = persist_cards(_payload(), store)["cards"][0]["candidate_id"]
    campaign = store.create_campaign(cid)
    store.transition(campaign["id"], "CLOSED")
    with pytest.raises(ValueError):
        store.transition(campaign["id"], "OPEN")


def test_execution_window_is_new_york_last_hour():
    ny = ZoneInfo("America/New_York")
    assert within_execution_window(datetime(2026, 7, 13, 15, 20, tzinfo=ny))
    assert not within_execution_window(datetime(2026, 7, 13, 14, 59, tzinfo=ny))
    assert not within_execution_window(datetime(2026, 7, 13, 15, 41, tzinfo=ny))
    assert not within_execution_window(datetime(2026, 7, 12, 15, 20, tzinfo=ny))
