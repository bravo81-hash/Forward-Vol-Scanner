from datetime import date, timedelta

import pytest

from core.historical import auto_historical_snapshot


AS_OF = date(2025, 3, 17)


def _rows(n, close_fn):
    days = []
    day = AS_OF
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    days.reverse()
    out = []
    for i, day in enumerate(days):
        close = float(close_fn(i))
        out.append((day, close * .998, close * 1.005, close * .995, close))
    return out


def _loader(ticker, _day):
    data = {
        "^GSPC": _rows(300, lambda i: 4800 + i * 3),
        "^VIX": _rows(252, lambda i: 14 + (i % 40) * .15),
        "^VIX9D": _rows(80, lambda _i: 23),
        "^VIX3M": _rows(80, lambda _i: 19),
        "^SKEW": _rows(80, lambda _i: 140),
    }
    return data[ticker]


def test_auto_snapshot_derives_regime_and_proxies():
    snap = auto_historical_snapshot("SPX", AS_OF, loader=_loader)
    assert snap["spot"] == 5694
    assert snap["iv30"] > 0
    assert snap["term"] == "INVERTED FRONT"
    assert snap["rr25"] > 0
    assert snap["skew_state"] == "RICH PUT PROXY"
    assert snap["confidence"] == "HIGH"
    assert snap["entry_date"] == "2025-03-17"
    assert snap["data_cutoff"] == "2025-03-14"


def test_optional_proxy_failure_uses_conservative_fallback():
    def loader(ticker, day):
        if ticker in {"^VIX9D", "^VIX3M", "^SKEW"}:
            raise RuntimeError("not published")
        return _loader(ticker, day)

    snap = auto_historical_snapshot("SPX", AS_OF, loader=loader)
    assert snap["term"] == "FLAT"
    assert snap["rr25"] == 0
    assert snap["confidence"] == "LOW"


def test_required_history_failure_is_explicit():
    with pytest.raises(RuntimeError, match="not enough historical price data"):
        auto_historical_snapshot("SPX", AS_OF, loader=lambda _ticker, _day: [])
