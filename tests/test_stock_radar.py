import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.events import macro_risk_gate
from core.price_action import enrich_ideas, validate_feed
from core.reprice import assess_liquidity
from core.stock_data import scan_stocks
from execution.stock_orders import approve_lots, make_stageable
from selection.stock_radar import (apply_earnings, monthly_expiry_near,
                                   rank_ideas, trigger_state)
from stock_radar import _account_profile, _live_selection, due_cadences
from store.radar import RadarStore


def test_mock_scan_returns_active_five_and_up_to_five_reserves():
    out = scan_stocks(cadence="daily", source="mock", limit=10,
                      today=date(2026, 7, 20))
    assert 5 <= len(out["candidates"]) <= 10
    assert all(x["list_role"] == "ACTIVE" for x in out["candidates"][:5])
    assert all(x["list_role"] == "RESERVE" for x in out["candidates"][5:])
    assert len(out["research_pool"]) >= len(out["candidates"])
    assert out["price_action_feed"]["status"] == "PRACTICE"
    assert out["price_action_feed"]["authority"] == "SHADOW_ONLY"
    for idea in out["candidates"]:
        assert idea["session"] == "2026-07-20"
        assert idea["score"] <= 100
        assert idea["trigger"]["operator"] in (">=", "<=")
        assert idea["tradingview_url"].startswith("https://www.tradingview.com/")
        assert "optionstrat.com/build/custom" in idea["strategy"]["optionstrat_url"]
        assert len(idea["strategy"]["legs_raw"]) == 2


def test_price_action_feed_is_freshness_checked_and_context_only():
    now = datetime(2026, 7, 21, 20, tzinfo=timezone.utc)
    base = {"schema_version": 1, "market": "us", "authority": "context_only",
            "generated": "2026-07-20T23:30:00Z", "rows": []}
    assert validate_feed(base, now=now)["status"] == "FRESH"
    stale = {**base, "generated": "2026-07-10T23:30:00Z"}
    assert validate_feed(stale, now=now)["status"] == "STALE"
    with pytest.raises(ValueError, match="context-only"):
        validate_feed({**base, "authority": "orders"}, now=now)


def test_price_action_annotations_do_not_modify_primary_score_or_rank():
    ideas = [
        {"symbol": "A", "direction": "BULL", "score": 91.0, "rank": 1},
        {"symbol": "B", "direction": "BEAR", "score": 88.0, "rank": 2},
        {"symbol": "C", "direction": "BULL", "score": 86.0, "rank": 3},
        {"symbol": "D", "direction": "BULL", "score": 84.0, "rank": 4},
    ]
    feed = {"bench": {"state": "CHOP_NO_EDGE"}, "rows": [
        {"ticker": "A", "signal": "S1", "side": "long", "evidence_tier": "CONTEXT"},
        {"ticker": "B", "signal": "S2", "side": "long", "evidence_tier": "CAUTION"},
        {"ticker": "C", "signal": "S3", "side": "neutral", "evidence_tier": "PREFERRED"},
        {"ticker": "D", "signal": "S4", "side": "long", "evidence_tier": "EXPERIMENTAL"},
    ]}
    out, meta = enrich_ideas(ideas, feed, {"status": "FRESH"})
    assert [x["score"] for x in out] == [91.0, 88.0, 86.0, 84.0]
    assert [x["rank"] for x in out] == [1, 2, 3, 4]
    assert [x["price_action"]["status"] for x in out] == [
        "CONFIRM", "CONFLICT", "NEUTRAL_RESEARCH", "EXPERIMENTAL"]
    assert out[0]["price_action"]["shadow_score"] == 93.0
    assert meta["matched_count"] == 4 and meta["authority"] == "SHADOW_ONLY"


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


def test_earnings_inside_hold_is_excluded_unless_event_trade():
    idea = {"risk_flags": [], "event_lock": False}
    locked = apply_earnings(idea, date(2026, 8, 10), date(2026, 7, 20))
    assert locked["earnings"]["status"] == "INSIDE_HOLD"
    assert locked["event_lock"] is True
    event = apply_earnings({**idea, "event_trade": True},
                           date(2026, 8, 10), date(2026, 7, 20))
    assert event["event_lock"] is False


def test_macro_gate_blocks_before_fomc_and_halves_post_release():
    ny = ZoneInfo("America/New_York")
    before = macro_risk_gate(datetime(2026, 7, 29, 13, 30, tzinfo=ny))
    after = macro_risk_gate(datetime(2026, 7, 29, 15, 0, tzinfo=ny))
    assert before["action"] == "BLOCK" and before["size_multiplier"] == 0
    assert after["action"] == "SIZE_DOWN" and after["size_multiplier"] == .5


def test_strict_option_liquidity_requires_oi_volume_and_nbbo():
    legs = [{"expiry": "2026-09-18", "strike": 100.0, "cp": "C"}]
    key = ("2026-09-18", 100.0, "C")
    low = assess_liquidity(legs, {key: {"bid": 2, "ask": 2.1, "mid": 2.05,
                                                "oi": 99, "volume": 9}},
                           enforce_depth=True)
    assert low["flagged"] and low["low_oi_legs"] and low["low_volume_legs"]
    good = assess_liquidity(legs, {key: {"bid": 2, "ask": 2.1, "mid": 2.05,
                                                 "oi": 100, "volume": 10}},
                            enforce_depth=True)
    assert good["flagged"] is False


def test_daily_and_cluster_risk_gates_block_new_stage():
    card = {"max_loss": -2, "cash_required": 200}
    idea = {"earnings": {"status": "CLEAR"}, "cluster": "mega_cap_tech"}
    trigger = {"state": "TRIGGERED"}
    usage = {"count": 1, "risk_amount": 200, "clusters": ["mega_cap_tech"]}
    checked = make_stageable(card, idea, 100_000, trigger, 10_000,
                             session_usage=usage)
    assert checked["permitted"] is False
    assert any("correlated factor cluster" in x for x in checked["blocks"])
    full = make_stageable(card, {**idea, "cluster": "retail"}, 100_000,
                          trigger, 10_000,
                          session_usage={"count": 2, "risk_amount": 1000,
                                         "clusters": []})
    assert full["governor"]["approved_lots"] == 0
    assert any("maximum two" in x for x in full["blocks"])


def test_challenger_is_visible_but_shadow_only(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "challenger.sqlite"))
    import store.radar as radar_module
    radar_module._STORE = None
    watch = scan_stocks(cadence="daily", source="mock", today=date(2026, 7, 20))
    saved = radar_module.radar_store().save(watch)
    quotes = {x["symbol"]: {"price": x["price"], "fresh": True}
              for x in saved["research_pool"]}
    # Force a reserve close to its trigger and depress the active floor.
    if len(saved["research_pool"]) > 5:
        reserve = saved["research_pool"][5]
        quotes[reserve["symbol"]]["price"] = reserve["trigger"]["price"]
        saved["research_pool"][4]["score"] = 50
    ny = ZoneInfo("America/New_York")
    rows, meta = _live_selection(saved, quotes, "daily",
                                 datetime(2026, 7, 21, 15, 5, tzinfo=ny))
    assert meta["phase"] == "FROZEN" and len(rows) <= 10
    assert all(x["shadow_only"] for x in rows if x["list_role"] == "CHALLENGER")


def test_shadow_outcomes_log_all_requested_horizons(tmp_path):
    store = RadarStore(tmp_path / "outcomes.sqlite")
    idea = {"symbol": "TEST", "rank": 1, "direction": "BULL",
            "trigger": {"price": 100}, "invalidation": 95, "target": 110}
    store.record_shadow_candidates("RAD-X", "2026-01-02", "v1_static", [idea])
    bars = []
    start = date(2026, 1, 3)
    for i in range(22):
        close = 101 + i * .25
        bars.append({"date": (start + timedelta(days=i)).isoformat(),
                     "open": close, "high": close + 1, "low": close - 1,
                     "close": close, "volume": 1})
    update = store.update_outcomes({"TEST": bars})
    assert update["outcomes_updated"] == 5
    summary = store.evidence_summary()
    assert [x["horizon_days"] for x in summary] == [1, 3, 5, 10, 20]


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
    assert len(payload["candidates"]) <= 10
    assert payload["price_action_feed"]["status"] == "PRACTICE"

    latest = client.get("/api/stocks/latest?cadence=daily").get_json()
    assert latest["snapshot_id"] == payload["snapshot_id"]
    monitored = client.get(
        "/api/stocks/monitor?cadence=daily&mode=mock&account=MOCK-A&nlv=100000"
    ).get_json()
    assert monitored["mode"] == "mock" and monitored["triggered"] == 1
    assert monitored["price_action_feed"]["status"] == "PRACTICE"
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
