import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.stock_data import scan_stocks
from execution.stock_orders import approve_lots, make_stageable
from selection.stock_radar import monthly_expiry_near, rank_ideas, trigger_state
from stock_radar import _account_profile, due_cadences


def test_mock_scan_returns_only_top_five_and_exact_trade_tools():
    out = scan_stocks(cadence="daily", source="mock", limit=10,
                      today=date(2026, 7, 20))
    assert 1 <= len(out["candidates"]) <= 5
    for idea in out["candidates"]:
        assert idea["session"] == "2026-07-20"
        assert idea["score"] <= 100
        assert idea["trigger"]["operator"] in (">=", "<=")
        assert idea["tradingview_url"].startswith("https://www.tradingview.com/")
        assert "optionstrat.com/build/custom" in idea["strategy"]["optionstrat_url"]
        assert len(idea["strategy"]["legs_raw"]) == 2


def test_weekly_scan_is_capped_at_ten_and_diversified():
    out = scan_stocks(cadence="weekly", source="mock", today=date(2026, 7, 17))
    assert len(out["candidates"]) <= 10
    sectors = {}
    clusters = {}
    for idea in out["candidates"]:
        sectors[idea["sector"]] = sectors.get(idea["sector"], 0) + 1
        clusters[idea["cluster"]] = clusters.get(idea["cluster"], 0) + 1
    assert max(sectors.values()) <= 2
    assert max(clusters.values()) <= 2


def test_trigger_requires_fresh_quote_and_execution_window():
    idea = {"direction": "BULL", "trigger": {"price": 100}, "atr": 4,
            "invalidation": 95,
            "event_lock": False}
    assert trigger_state(idea, 101, fresh=True, in_last_hour=True)["state"] == "TRIGGERED"
    assert trigger_state(idea, 101, fresh=True, in_last_hour=False)["state"] == "CROSSED"
    assert trigger_state(idea, 101, fresh=False, in_last_hour=True)["state"] == "STALE"
    assert trigger_state(idea, 99, fresh=True, in_last_hour=True)["state"] == "ARMED"
    assert trigger_state(idea, 102, fresh=True, in_last_hour=True)["state"] == "EXTENDED"
    assert trigger_state(idea, 94, fresh=True, in_last_hour=True)["state"] == "INVALID"


def test_monthly_expiry_leaves_tenor_after_thirty_day_hold():
    today = date(2026, 7, 20)
    exp = monthly_expiry_near(today)
    assert 40 <= (exp - today).days <= 85
    assert (exp - today).days - 30 >= 10


def test_rank_hysteresis_and_sector_cap():
    base = {"features": {"dollar_volume": 1_000_000_000}, "event_lock": False,
            "score_components": {}}
    ideas = [{**base, "symbol": f"T{i}", "score": 90 - i, "sector": "Tech",
              "cluster": f"c{i}"} for i in range(5)]
    picked = rank_ideas(ideas, "weekly", previous_symbols={"T2"}, limit=5)
    assert len(picked) == 2
    assert any(x["symbol"] == "T2" and x["score_components"]["watchlist_stability"] == 2
               for x in picked)


def test_due_cadences_use_new_york_close_and_friday(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "due.sqlite"))
    import store.radar as radar_module
    radar_module._STORE = None
    ny = ZoneInfo("America/New_York")
    assert due_cadences(datetime(2026, 7, 17, 16, 9, tzinfo=ny)) == []
    assert due_cadences(datetime(2026, 7, 17, 16, 10, tzinfo=ny)) == ["daily", "weekly"]


def test_live_account_must_be_explicit_and_user_cannot_inflate_nlv(monkeypatch):
    rows = [{"account": "A", "nlv": 80_000, "available_funds": 12_000},
            {"account": "B", "nlv": 140_000, "available_funds": 20_000}]
    monkeypatch.setattr("stock_radar.list_accounts", lambda _ib: rows)
    with pytest.raises(ValueError, match="select the IBKR account"):
        _account_profile(object(), None, 250_000)
    profile = _account_profile(object(), "A", 250_000)
    assert profile["nlv"] == 80_000


def test_available_funds_can_reduce_approved_lots_to_zero():
    card = {"max_loss": -4, "cash_required": 400}
    assert approve_lots(card, 100_000, available_funds=10_000)["approved_lots"] == 1
    blocked = approve_lots(card, 100_000, available_funds=300)
    assert blocked["approved_lots"] == 0
    assert "available funds" in blocked["binding"]


def test_existing_position_blocks_radar_staging():
    card = {"max_loss": -2, "cash_required": 200}
    idea = {"earnings": {"status": "CLEAR"}}
    trigger = {"state": "TRIGGERED"}
    checked = make_stageable(card, idea, 100_000, trigger, 10_000,
                             position_overlap=True)
    assert checked["permitted"] is False
    assert any("existing position" in x for x in checked["blocks"])


def test_stock_radar_endpoints_mock(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "radar.sqlite"))
    import store.radar as radar_module
    import store.campaigns as campaign_module
    radar_module._STORE = None
    campaign_module._STORE = None
    import webapp
    client = webapp.app.test_client()

    scan = client.post("/api/stocks/scan", json={"cadence": "daily", "source": "mock"})
    assert scan.status_code == 200
    payload = scan.get_json()
    assert payload["snapshot_id"].startswith("RAD-")
    assert len(payload["candidates"]) <= 5

    latest = client.get("/api/stocks/latest?cadence=daily").get_json()
    assert latest["snapshot_id"] == payload["snapshot_id"]
    monitored = client.get(
        "/api/stocks/monitor?cadence=daily&mode=mock&account=MOCK-A&nlv=100000"
    ).get_json()
    assert monitored["mode"] == "mock" and monitored["triggered"] == 1
    first = monitored["candidates"][0]
    assert first["monitor"]["state"] == "TRIGGERED"
    assert first["trade_card"]["candidate_id"]

    staged = client.post("/api/stocks/stage", json={
        "candidate_id": first["trade_card"]["candidate_id"], "quantity": 1,
    }).get_json()
    assert staged["status"] == "MockStaged" and staged["transmit"] is False

    blocked_live = client.get(
        "/api/stocks/monitor?cadence=daily&mode=yf&account=MOCK-A&nlv=100000"
    )
    assert blocked_live.status_code == 400
    assert "practice watchlists" in blocked_live.get_json()["error"]


def test_stock_page_is_linked_from_default_desk():
    import webapp
    client = webapp.app.test_client()
    assert "Stock Opportunity Radar" in client.get("/stocks").get_data(as_text=True)
    assert 'href="/stocks"' in client.get("/").get_data(as_text=True)
