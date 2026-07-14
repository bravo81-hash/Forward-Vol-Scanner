from management.engine import advise_campaign


def _campaign(state="OPEN"):
    return {"state": state, "quantity": 1,
            "card": {"strategy": "balanced_fly", "evidence": {"status": "HYPOTHESIS"},
                     "manage": {"pt_dollars": 100, "sl_dollars": -80}}}


def test_management_profit_and_time_exits():
    assert advise_campaign(_campaign(), {"pnl_dollars": 110}, {"fresh": True})["action"] == "EXIT"
    assert advise_campaign(_campaign(), {"pnl_dollars": 0, "min_short_dte": 7}, {"fresh": True})["action"] == "EXIT"


def test_management_degross_and_delta_repair():
    out = advise_campaign(_campaign(), {"pnl_dollars": 0},
                          {"fresh": True, "term": "INVERTED FRONT"})
    assert out["action"] == "REDUCE"
    out = advise_campaign(_campaign(), {"delta": 12, "delta_limit": 5}, {"fresh": True})
    assert out["action"] == "ADJUST"


def test_management_stale_data_blocks_action():
    out = advise_campaign(_campaign(), {}, {"fresh": False})
    assert out["action"] == "DATA INVALID" and not out["stage_allowed"]


def test_eligible_manual_campaign_requires_review():
    out = advise_campaign(_campaign("ELIGIBLE"), {}, {"fresh": True})
    assert out["action"] == "REVIEW"
