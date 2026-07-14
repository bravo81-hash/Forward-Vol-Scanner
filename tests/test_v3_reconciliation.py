from campaign.grouping import reconcile_positions
from execution.candidates import persist_cards
from store.campaigns import CampaignStore


def _payload():
    return {"symbol": "SPX", "spot": 6000, "mode": "mock", "policy_id": "x",
            "data": {"session": "2026-07-14", "fresh": True}, "inputs": {},
            "action": "TEST", "account": {"account": "MOCK-B"},
            "cards": [{"strategy": "balanced_fly", "label": "fly",
                       "legs": ["-2 Aug21 6000P"],
                       "legs_raw": [{"cp": "P", "strike": 6000, "expiry": "2026-08-21", "qty": -2}],
                       "net_mid": 1, "max_loss": -10, "cash_required": 1000,
                       "greeks": {}, "evidence": {"status": "HYPOTHESIS"}, "blocks": [],
                       "tws_stage_allowed": True, "governor": {"approved_lots": 2}}]}


def test_position_grouping_exact_partial_and_unassigned(tmp_path):
    store = CampaignStore(tmp_path / "x.sqlite")
    cid = persist_cards(_payload(), store)["cards"][0]["candidate_id"]
    campaign = store.create_campaign(cid)
    exact = reconcile_positions([campaign], [{"symbol": "SPX", "cp": "P", "strike": 6000,
                                               "expiry": "2026-08-21", "qty": -2}])
    assert exact["complete"] and len(exact["exact"]) == 1
    partial = reconcile_positions([campaign], [{"symbol": "SPX", "cp": "P", "strike": 6000,
                                                 "expiry": "2026-08-21", "qty": -1}])
    assert partial["partial"] and not partial["complete"]
    missing = reconcile_positions([campaign], [{"symbol": "SPX", "cp": "C", "strike": 6100,
                                                 "expiry": "2026-08-21", "qty": 1}])
    assert missing["unassigned"]


def test_order_partial_and_full_fill_reconciliation(tmp_path):
    store = CampaignStore(tmp_path / "x.sqlite")
    cid = persist_cards(_payload(), store)["cards"][0]["candidate_id"]
    campaign = store.create_campaign(cid)
    order = store.record_order(cid, 2, {"status": "MockStaged", "limit": 1.25}, campaign["id"])
    assert order["transmit"] == 0
    order = store.record_fill(order["id"], 1, 1.20, 3.0)
    assert order["status"] == "PartiallyFilled"
    order = store.record_fill(order["id"], 1, 1.30, 3.0)
    assert order["status"] == "Filled" and order["average_fill"] == 1.25
    assert order["fees"] == 6.0
