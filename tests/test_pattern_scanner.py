import numpy as np
import pandas as pd
import json

from pattern_scanner.patterns import PatternCandidate, classify, detect_all
from pattern_scanner.scanner import live_pattern_status, scan_patterns


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
        assert row["sector"] == "Technology"
        assert row["review"] == "REQUIRED"


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


if __name__ == "__main__":
    tests = [value for name, value in sorted(globals().items())
             if name.startswith("test_") and callable(value)
             and value.__code__.co_argcount == 0]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"{len(tests)} pattern tests passed")
