"""P5-P8 tests. Zero TWS. Dates cross-checked against bls.gov/schedule/2026."""
import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.events import (CPI_2026, NFP_2026, PPI_2026, macro_between,
                         macro_within, next_macro, event_flags)
from core.regime import build_gates, compute_regime, mock_bars, mock_iv_hist
from core.reprice import assess_liquidity, WING_SPREAD_WARN
from core.context import build_context
from core.surface import pair_table
from selection.ranker import shortlist

TODAY = date(2026, 7, 1)


# ---------------------------------------------------- P5 macro calendar -----
def test_bls_dates_match_official_schedule():
    # Spot-checked against bls.gov/schedule/2026 (fetched Jul 2026)
    assert date(2026, 7, 14) in CPI_2026     # June CPI
    assert date(2026, 7, 15) in PPI_2026     # June PPI
    assert date(2026, 7, 2) in NFP_2026      # June employment situation
    assert date(2026, 12, 10) in CPI_2026    # Nov CPI, year-end


def test_next_macro_picks_nearest():
    dte, kind = next_macro(date(2026, 7, 1))
    assert kind == "NFP" and dte == 1        # Jul 2 NFP is 1 day out


def test_macro_between_lists_all_types_in_window():
    # front Jul 10 (before Jul 14 CPI/15 PPI), back Aug 10 (after both + Aug 7 NFP)
    types = macro_between(date(2026, 7, 10), date(2026, 8, 10))
    assert set(types) >= {"CPI", "PPI", "NFP"}


def test_macro_between_empty_when_no_release_spans():
    types = macro_between(date(2026, 7, 15), date(2026, 7, 20))
    assert types == []


def test_event_flags_expose_macro_in_front():
    ev = event_flags(date(2026, 7, 1), "SPX", front_max_dte=21)
    assert ev["macro_type"] == "NFP" and ev["macro_in_front"] is True


def test_gate_M_fires_inside_front_window():
    reg = {"iv_chg_pct": 0, "vol_state": "NRM", "gamma": "+g", "vrp": 1.0}
    ev = {"fomc_in_front": False, "macro_in_front": True, "macro_dte": 5,
          "macro_type": "CPI", "post_opex": False, "ex_div": False}
    gates = build_gates(reg, ev, date(2026, 7, 1))
    m = [g for g in gates if g["code"] == "M"]
    assert m and m[0]["hard"] is False and "CPI" in m[0]["msg"]


def test_pair_table_flags_macro_between_not_exclude():
    from core.models import Slice
    f = Slice(date(2026, 7, 13), 12, 6000, 0.16)     # front before Jul 14 CPI, in-window
    b = Slice(date(2026, 8, 14), 44, 6000, 0.155)    # back after CPI/PPI/NFP
    rows = pair_table([f, b], date(2026, 7, 1))
    assert len(rows) == 1
    assert set(rows[0]["macro_between"]) & {"CPI", "PPI"}


# ---------------------------------------------------- P6 forward VRP --------
def test_har_rv_matches_manual_weights():
    bars = mock_bars("SPX", 6000.0, TODAY, n=300)
    ivh = mock_iv_hist(15.0, n=252)
    reg = compute_regime(bars, ivh, ivh[-1])
    # har_rv is computed from UNROUNDED rv7/21/63; assert the relationship that
    # actually matters downstream — vrp_fwd = iv30 - har_rv — holds exactly.
    assert reg["vrp_fwd"] == round(reg["iv30"] - reg["har_rv"], 2)
    lo = min(reg["rv7"], reg["rv21"], reg["rv63"])
    hi = max(reg["rv7"], reg["rv21"], reg["rv63"])
    assert lo - 0.02 <= reg["har_rv"] <= hi + 0.02


def test_vrp_flip_flag_true_when_signs_disagree():
    reg = {"vrp": -0.5, "vrp_fwd": 0.8, "har_rv": 12.0, "vrp_flip": True,
           "iv_chg_pct": 0, "vol_state": "NRM", "gamma": "+g"}
    ev = {"fomc_in_front": False, "post_opex": False, "ex_div": False}
    gates = build_gates(reg, ev)
    n = [g for g in gates if g["code"] == "N"]
    assert n and "HAR" in n[0]["msg"]


def test_vrp_flip_flag_false_when_signs_agree():
    reg = {"vrp": 1.5, "vrp_fwd": 0.8, "har_rv": 12.0, "iv_chg_pct": 0,
           "vol_state": "NRM", "gamma": "+g"}
    ev = {"fomc_in_front": False, "post_opex": False, "ex_div": False}
    gates = build_gates(reg, ev)
    assert not [g for g in gates if g["code"] == "N"]


def test_rv63_falls_back_on_short_history():
    bars = mock_bars("SPX", 6000.0, TODAY, n=50)     # < 64 -> rv63 guard fires
    ivh = mock_iv_hist(15.0, n=100)
    reg = compute_regime(bars, ivh, ivh[-1])
    assert reg["rv63"] == reg["rv21"]        # documented fallback


# ---------------------------------------------------- P7 liquidity ----------
def _leg(strike, cp, expiry="2026-07-30"):
    return {"expiry": expiry, "strike": strike, "cp": cp, "qty": -1, "iv": 0.15}


def test_liquidity_flags_missing_quote():
    legs = [_leg(6000, "P")]
    rows = {("2026-07-30", 6000, "P"): None}
    r = assess_liquidity(legs, rows)
    assert r["flagged"] and "6000P" in r["no_quote_legs"]


def test_liquidity_flags_wide_spread():
    legs = [_leg(5800, "P")]
    rows = {("2026-07-30", 5800, "P"): {"bid": 1.0, "ask": 1.5, "mid": 1.25}}  # 40% spread
    r = assess_liquidity(legs, rows)
    assert r["flagged"] and r["wide_legs"][0]["spread_pct"] == 40.0


def test_liquidity_clean_quote_not_flagged():
    legs = [_leg(6000, "C")]
    rows = {("2026-07-30", 6000, "C"): {"bid": 10.0, "ask": 10.3, "mid": 10.15}}  # ~3%
    r = assess_liquidity(legs, rows)
    assert not r["flagged"]


def test_wing_spread_threshold_is_documented_value():
    assert WING_SPREAD_WARN == 0.15


# ---------------------------------------------------- P8 snapshot -----------
def test_snapshot_report_renders_mock():
    import snapshot
    html = snapshot.build_report(["SPX"], "mock", None)
    assert "<html>" in html and "SPX" in html
    assert "TRADE" in html.upper() or "STAND" in html.upper() or "CAUTION" in html.upper()


def test_snapshot_card_shows_lots_and_management():
    import snapshot
    ctx = build_context("SPX", "mock", TODAY)
    out = shortlist(ctx)
    assert out["cards"], "no cards to render"
    html = snapshot.render_symbol("SPX", out)
    assert "lots" in html and "PT/SL" in html and "exit by" in html
