import json
from pathlib import Path

from selection.direction import BUY_VRP, SELL_VRP


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tradingview/parity_fixture.json").read_text())
PINE = (ROOT / FIXTURE["pine_file"]).read_text()


def test_fvs_console_gate1_and_har_parity():
    assert FIXTURE["gate1"]["sell_vol_min_v"] == SELL_VRP
    assert FIXTURE["gate1"]["buy_vol_max_v"] == BUY_VRP
    assert "harRV   = 0.5 * rv7 + 0.3 * rv21 + 0.2 * rv63" in PINE
    assert 'vrpFwd >= 3 ? "SELL VOL"' in PINE
    assert 'vrpFwd <= -2 ? "BUY VOL"' in PINE
    assert 'play = eventRich ? "EVENT VOL"' in PINE


def test_fvs_console_scope_and_compatibility_header():
    assert FIXTURE["source_commit"] in PINE
    assert "//@version=6" in PINE
    assert "CPI/PPI/NFP are NOT embedded" in PINE
    assert "TIMEEDGE / OTM CALENDAR" in PINE
    assert "CHECK TIMEZONE" in PINE
    assert "Monday close is the default campaign entry" in PINE
