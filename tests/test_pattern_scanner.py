import numpy as np
import pandas as pd
import json
import threading
import time

from pattern_scanner.patterns import PatternCandidate, classify, detect_all
from pattern_scanner.scanner import _sector_for, live_pattern_status, scan_patterns


def bars(close, volume=None):
    close = np.asarray(close, dtype=float)
    n = len(close)
    volume = np.asarray(volume if volume is not None else np.full(n, 1_000_000), dtype=float)
    wiggle = np.maximum(close * 0.006, 0.35)
    return pd.DataFrame({
        "open": np.r_[close[0], close[:-1]],
        "high": close + wiggle,
        "low": close - wiggle,
        "close": close,
        "volume": volume,
    }, index=pd.bdate_range("2024-01-02", periods=n))


def test_status_requires_material_atr_penetration_and_supports_retest():
    d = bars(np.r_[np.full(98, 99.0), 100.1, 100.2])
    c = PatternCandidate("X", "P1", "Base", "long", .8, 100, 95, 105, 0, 99)
    classify(c, d, atr=2.0)
    assert c.status == "NEAR_TRIGGER"  # 0.1 is not a 0.25 ATR confirmation

    d2 = bars(np.r_[np.full(95, 99.0), 101.0, 101.2, 100.8, 100.3, 100.1])
    c2 = PatternCandidate("X", "P1", "Base", "long", .8, 100, 95, 105, 0, 99)
    classify(c2, d2, atr=2.0)
    assert c2.status == "RETESTING"


def test_retest_ignores_breaks_before_the_pattern_started():
    d = bars(np.r_[101.0, 101.2, np.full(94, 97.0), 99.5, 99.7, 99.9, 100.1])
    c = PatternCandidate("X", "P4", "Double bottom", "long", .8,
                         100, 94, 106, 20, 99)
    classify(c, d, atr=2.0)
    assert c.status == "NEAR_TRIGGER"
    assert c.breakout_age is None


def test_flat_base_and_triangle_geometry_are_detected():
    prior = np.linspace(70, 100, 80)
    base = np.array([96, 98, 99.6, 97, 98.8, 99.7, 97.5, 99.2, 99.8, 98.2,
                     99.1, 99.7, 98.8, 99.4, 99.8, 99.0, 99.5, 99.9, 99.2, 99.6,
                     99.8, 99.4, 99.7, 99.9, 99.6, 99.8, 99.7, 99.85, 99.8, 99.9,
                     100.7])
    found = detect_all("BASE", bars(np.r_[prior, base]))
    assert any(x.code in ("P1", "P5") for x in found)
    assert any(x.status in ("NEAR_TRIGGER", "CLOSE_CONFIRMED", "RETESTING") for x in found)


def test_double_bottom_geometry_is_detected():
    lead = np.linspace(120, 105, 80)
    first = np.r_[np.linspace(105, 90, 12), np.linspace(90, 104, 12)]
    middle = np.linspace(104, 108, 8)
    second = np.r_[np.linspace(108, 91, 14), np.linspace(91, 107, 15)]
    finish = np.r_[np.linspace(107, 107.5, 7), 110.5]
    found = detect_all("DB", bars(np.r_[lead, first, middle, second, finish]))
    assert any(x.code == "P4" for x in found)


def test_cup_handle_geometry_is_detected():
    prior = np.linspace(60, 100, 70)
    t = np.linspace(-1, 1, 80)
    cup = 75 + 25 * t * t
    handle = np.r_[np.linspace(99, 94, 8), np.linspace(94, 97, 7), 101]
    found = detect_all("CUP", bars(np.r_[prior, cup, handle]))
    row = next(x for x in found if x.code == "P2")
    assert row.status == "NEAR_TRIGGER"
    assert "handle" in row.detail
    json.dumps(row.row())  # all numpy geometry values are export-safe natives


def test_head_and_shoulders_both_directions_are_detected():
    lead = np.linspace(120, 100, 60)
    ihs = np.r_[np.linspace(100, 90, 10), np.linspace(90, 102, 10),
                np.linspace(102, 82, 13), np.linspace(82, 103, 13),
                np.linspace(103, 91, 11), np.linspace(91, 104, 12), 106]
    assert any(x.code == "P3" for x in detect_all("IHS", bars(np.r_[lead, ihs])))

    lead = np.linspace(80, 100, 60)
    hs = np.r_[np.linspace(100, 110, 10), np.linspace(110, 98, 10),
               np.linspace(98, 118, 13), np.linspace(118, 97, 13),
               np.linspace(97, 109, 11), np.linspace(109, 96, 12), 94]
    assert any(x.code == "P6" for x in detect_all("HS", bars(np.r_[lead, hs])))


def test_ascending_triangle_geometry_is_detected():
    prior = np.linspace(70, 92, 70)
    seq = []
    for low in (90, 92, 94, 96):
        seq.extend(np.linspace(seq[-1] if seq else 92, 100, 6))
        seq.extend(np.linspace(100, low, 6))
    seq.extend(np.linspace(96, 101.5, 7))
    found = detect_all("TRI", bars(np.r_[prior, seq]))
    assert any(x.code == "P5" for x in found)


def test_flag_geometry_is_detected():
    lead = np.linspace(80, 90, 80)
    pole = np.linspace(90, 105, 10)
    flag = np.linspace(104.5, 101.5, 9)
    last = np.array([102.5, 105.8])
    volume = np.r_[np.full(len(lead), 1_000_000), np.full(len(pole), 2_000_000),
                   np.full(len(flag) + len(last), 900_000)]
    found = detect_all("FLAG", bars(np.r_[lead, pole, flag, last], volume))
    assert any(x.code == "P8" for x in found)


def test_pipeline_returns_only_actionable_by_default_and_scores_context():
    prior = np.linspace(70, 100, 100)
    base = np.r_[np.tile([97.5, 99.8, 98.2, 99.7], 8), 100.8]
    d = bars(np.r_[prior, base])
    result = scan_patterns({"AAA": d}, bench_daily=d,
                           sector_daily={"XLK": d}, min_geometry=0.2,
                           min_context=0, geometry_limit=100, context_limit=20,
                           final_limit=10)
    assert result.geometry_count >= 1
    assert all(r["status"] != "FORMING" for r in result.rows)
    if result.rows:
        row = result.rows[0]
        assert 0 <= row["context_score"] <= 1
        assert row["sector"] == "Unknown"  # correlation is a proxy, never an identity label
        assert row["sector_source"] == "correlation_proxy"
        assert row["review"] == "REQUIRED"
        assert row["chart"]["dates"]
        assert all("date" in point for point in row["points"].values())


def test_live_overlay_separates_intraday_trigger_from_close_confirmation():
    row = {"side": "long", "trigger": 100.0, "invalidation": 95.0,
           "atr": 2.0, "status": "NEAR_TRIGGER", "breakout_age": None}
    assert live_pattern_status(row, 100.2)[0] == "NEAR_TRIGGER"
    assert live_pattern_status(row, 100.6)[0] == "TRIGGERED_INTRADAY"
    assert live_pattern_status(row, 94.0)[0] == "FAILED"


def test_pattern_module_page_and_api(monkeypatch):
    import pattern_scanner.service as service
    from webapp import app

    expected = {
        "module": "price_action_patterns", "rows": [], "universe_requested": 4,
        "symbols_with_bars": 4, "liquid_symbols": 4, "geometry_count": 2,
        "context_count": 1, "actionable_count": 1,
    }
    monkeypatch.setattr(service, "run_pattern_scan", lambda **kwargs: expected)
    client = app.test_client()
    assert client.get("/patterns").status_code == 200
    response = client.get("/api/patterns/scan?source=mock&tickers=AAPL,MSFT")
    assert response.status_code == 200
    assert response.get_json()["module"] == "price_action_patterns"
    assert response.get_json()["scan_id"]


def test_live_endpoint_reuses_cached_shortlist_without_rerunning_scan(monkeypatch):
    import pattern_scanner.service as service
    from webapp import app, _pattern_scan_cache

    expected = {
        "module": "price_action_patterns", "rows": [{"ticker": "AAPL", "score": .8}],
        "universe_requested": 1, "symbols_with_bars": 1, "liquid_symbols": 1,
        "geometry_count": 1, "context_count": 1, "actionable_count": 1,
    }
    calls = {"scan": 0, "live": 0}
    def scan(**kwargs):
        calls["scan"] += 1
        return expected
    def live(rows):
        calls["live"] += 1
        return [{**rows[0], "live": 200.0, "live_status": "NEAR_TRIGGER"}], {"fresh": 1, "total": 1, "ok": True}, 0

    _pattern_scan_cache.clear()
    monkeypatch.setattr(service, "run_pattern_scan", scan)
    monkeypatch.setattr(service, "validate_pattern_rows", live)
    client = app.test_client()
    daily = client.get("/api/patterns/scan?source=mock")
    scan_id = daily.get_json()["scan_id"]
    response = client.post("/api/patterns/live", json={"scan_id": scan_id})
    assert response.status_code == 200
    assert response.get_json()["rows"][0]["live"] == 200.0
    assert calls == {"scan": 1, "live": 1}


def test_live_endpoint_rejects_missing_or_expired_scan():
    from webapp import app, _pattern_scan_cache
    _pattern_scan_cache.clear()
    response = app.test_client().post("/api/patterns/live", json={"scan_id": "missing"})
    assert response.status_code == 409
    assert "Run the daily scan again" in response.get_json()["error"]


def test_background_scan_job_avoids_long_lived_http_request(monkeypatch):
    import pattern_scanner.service as service
    from webapp import app, _pattern_scan_jobs

    release = threading.Event()
    expected = {
        "module": "price_action_patterns", "rows": [], "universe_requested": 2,
        "symbols_with_bars": 2, "liquid_symbols": 2, "geometry_count": 0,
        "context_count": 0, "actionable_count": 0,
    }

    def scan(**kwargs):
        release.wait(timeout=2)
        return expected

    _pattern_scan_jobs.clear()
    monkeypatch.setattr(service, "run_pattern_scan", scan)
    client = app.test_client()
    started = client.post("/api/patterns/scan/start?source=mock&tickers=AAPL,MSFT")
    assert started.status_code == 202
    job_id = started.get_json()["job_id"]
    assert client.get(f"/api/patterns/scan/status/{job_id}").status_code == 202

    release.set()
    for _ in range(100):
        response = client.get(f"/api/patterns/scan/status/{job_id}")
        if response.status_code == 200:
            break
        time.sleep(.01)
    assert response.status_code == 200
    assert response.get_json()["module"] == "price_action_patterns"
    assert response.get_json()["scan_id"]


def test_codespaces_disables_local_tws_validation(monkeypatch):
    from webapp import app

    monkeypatch.setenv("CODESPACES", "true")
    capabilities = app.test_client().get("/api/patterns/capabilities")
    assert capabilities.status_code == 200
    assert capabilities.get_json()["tws_validation"] is False
    response = app.test_client().post("/api/patterns/live", json={"scan_id": "unused"})
    assert response.status_code == 409
    assert "unavailable in Codespaces" in response.get_json()["error"]


def test_stale_breakout_and_target_hit_are_expired():
    stale = bars(np.r_[np.full(90, 99.0), 101.0, np.full(6, 101.2)])
    c = PatternCandidate("X", "P1", "Base", "long", .8, 100, 95, 120, 70, 89)
    classify(c, stale, atr=2.0)
    assert c.breakout_age == 6
    assert c.status == "EXPIRED"

    reached = bars(np.r_[np.full(95, 99.0), 101.0, 103.2])
    c2 = PatternCandidate("X", "P1", "Base", "long", .8, 100, 95, 103, 70, 94)
    classify(c2, reached, atr=2.0)
    assert c2.status == "EXPIRED"


def test_xbi_regression_old_triangle_is_expired_not_actionable():
    # Regression for the reported XBI false positive: 154.51 current versus a
    # 137.13 trigger and 149.19 target.  The target is already behind price.
    d = bars(np.r_[np.full(94, 135.0), 138.0, 145.0, 150.0, 154.51])
    xbi = PatternCandidate("XBI", "P5", "Ascending triangle", "long", .917,
                           137.13, 130.0, 149.19, 60, 93)
    classify(xbi, d, atr=4.63)
    assert xbi.status == "EXPIRED"


def test_price_ordering_sanity_gate_rejects_impossible_levels():
    d = bars(np.linspace(95, 101, 100))
    invalid = PatternCandidate("X", "P5", "Ascending triangle", "long", .9,
                               100, 102, 110, 60, 95)
    classify(invalid, d, atr=2.0)
    assert invalid.status == "INVALID"


def test_old_triangle_cannot_be_promoted_after_extension():
    prior = np.linspace(70, 92, 70)
    seq = []
    for low in (90, 92, 94, 96):
        seq.extend(np.linspace(seq[-1] if seq else 92, 100, 6))
        seq.extend(np.linspace(100, low, 6))
    # The textbook triangle broke long ago and price is now far beyond both
    # trigger and measured target.  It must not reappear as actionable.
    old = bars(np.r_[prior, seq, np.linspace(101, 125, 30)])
    assert not any(x.code == "P5" for x in detect_all("OLD", old))


def test_known_etf_sector_identity_is_not_guessed_by_correlation():
    d = bars(np.linspace(100, 110, 100))
    sector, _, _, _, source = _sector_for("XBI", d, {"XLI": d, "XLV": d}, 0.0)
    assert sector == "Health care"
    assert source == "identity"


def test_yahoo_history_requests_adjusted_prices(monkeypatch):
    import sys
    from types import SimpleNamespace
    from core.stock_data import histories_yf

    captured = {}
    frame = pd.DataFrame({"Open": [99.0], "High": [101.0], "Low": [98.0],
                          "Close": [100.0], "Volume": [1_000_000]},
                         index=pd.to_datetime(["2026-07-21"]))

    def download(*args, **kwargs):
        captured.update(kwargs)
        return frame

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(download=download))
    out = histories_yf(["ABC"], completed_only=False)
    assert captured["auto_adjust"] is True
    assert captured["timeout"] == 20
    assert captured["threads"] == 8
    assert out["ABC"][0]["close"] == 100.0


def test_pattern_service_always_requests_completed_daily_bars(monkeypatch):
    import pattern_scanner.service as service

    called = {}
    def stop_after_fetch(*args, **kwargs):
        called.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(service, "histories_yf", stop_after_fetch)
    try:
        service.run_pattern_scan(source="yf", tickers=["AAPL"], include_earnings=False)
    except RuntimeError as exc:
        assert str(exc) == "stop"
    assert called["completed_only"] is True


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)
             and value.__code__.co_argcount == 0]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} pattern tests passed")
