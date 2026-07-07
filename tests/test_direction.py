"""Direction module — Gate 1 / Gate 2 matrix and /api/direction route."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from core.models import Context, Slice
from selection.direction import (BUY_VRP, SELL_VRP, direction_verdict, gate1,
                                 gate2, vol_band)

TODAY = date(2026, 7, 7)


def mk_ctx(*, vrp_fwd=0.0, vrp=0.0, iv_pctl=50.0, vol_state="NRM",
           skew_rich=False, rr25=3.0, term_verdict="CONTANGO", bias=0,
           ivp_proxy=False, iv30=16.0, har_rv=16.0, slices=None) -> Context:
    reg = {"iv30": iv30, "iv_pctl": iv_pctl, "vol_state": vol_state,
           "vrp": vrp, "vrp_fwd": vrp_fwd, "har_rv": har_rv, "rv21": 14.0,
           "bias": bias, "trend": "RNG",
           "term": {"verdict": term_verdict, "skew_rich": skew_rich,
                    "rr25_30d": rr25}}
    if ivp_proxy:
        reg["ivp_proxy"] = True
    # slices far from any FOMC step so event_premium stays quiet unless given
    sl = slices or [
        Slice(expiry=TODAY + timedelta(days=d), dte=d, atm_strike=100.0,
              atm_iv=iv30 / 100) for d in (10, 24, 38)]
    return Context(symbol="TST", spot=100.0, today=TODAY, slices=sl,
                   strikes=[95.0, 100.0, 105.0], regime=reg,
                   events={}, gates=[], mode="mock",
                   data={"note": "test"})


def top(structs):
    return structs[0]["key"]


# ------------------------------------------------------------------ gate 1 --
def test_gate1_sell_vol_threshold():
    assert gate1(mk_ctx(vrp_fwd=SELL_VRP))["play"] == "SELL_VOL"
    assert gate1(mk_ctx(vrp_fwd=SELL_VRP - 0.1))["play"] == "DELTA"


def test_gate1_buy_vol_threshold():
    assert gate1(mk_ctx(vrp_fwd=BUY_VRP))["play"] == "BUY_VOL"
    assert gate1(mk_ctx(vrp_fwd=BUY_VRP + 0.1))["play"] == "DELTA"


def test_gate1_vrp_flip_caution():
    ctx = mk_ctx(vrp_fwd=0.5)
    ctx.regime["vrp_flip"] = True
    assert any("CAUTION" in w for w in gate1(ctx)["why"])


# ------------------------------------------------------- gate 2, long side --
def test_long_high_iv_steep_skew_put_credit_first():
    s = gate2(mk_ctx(vol_state="ELV", skew_rich=True, rr25=6.0), "long")
    assert top(s) == "credit_vertical"
    assert s[0]["label"] == "Put credit spread"
    assert "skew" in s[0]["why"]


def test_long_low_iv_call_debit_first():
    s = gate2(mk_ctx(vol_state="CMP"), "long")
    assert top(s) == "debit_vertical"
    assert s[0]["label"] == "Call debit spread"


def test_long_low_iv_inverted_front_calendar_second():
    s = gate2(mk_ctx(vol_state="CMP", term_verdict="INVERTED FRONT"), "long")
    assert [x["key"] for x in s][:2] == ["debit_vertical", "otm_calendar"]


def test_long_low_iv_contango_demotes_calendar():
    s = gate2(mk_ctx(vol_state="CMP", term_verdict="STEEP CONTANGO"), "long")
    assert s[-1]["key"] == "otm_calendar"


def test_long_nrm_inverted_front_calendar_first():
    s = gate2(mk_ctx(term_verdict="INVERTED FRONT"), "long")
    assert top(s) == "otm_calendar"
    assert s[0]["label"] == "OTM call calendar"


def test_long_nrm_skew_rich_put_credit_first():
    s = gate2(mk_ctx(skew_rich=True, rr25=6.0), "long")
    assert top(s) == "credit_vertical"


def test_long_nrm_flat_vrp_tiebreak():
    assert top(gate2(mk_ctx(vrp_fwd=1.0), "long")) == "credit_vertical"
    assert top(gate2(mk_ctx(vrp_fwd=-1.0), "long")) == "debit_vertical"


# ------------------------------------------------------ gate 2, short side --
def test_short_high_iv_steep_skew_fly_first_and_asymmetry_noted():
    s = gate2(mk_ctx(vol_state="ELV", skew_rich=True, rr25=6.0), "short")
    assert top(s) == "otm_butterfly"
    assert s[0]["label"] == "OTM put butterfly"
    ccs = next(x for x in s if x["key"] == "credit_vertical")
    assert "CHEAP" in ccs["why"]          # the index-skew asymmetry is explicit


def test_short_high_iv_normal_skew_call_credit_first():
    s = gate2(mk_ctx(vol_state="STR"), "short")
    assert top(s) == "credit_vertical"
    assert s[0]["label"] == "Call credit spread"


def test_short_low_iv_put_debit_first():
    s = gate2(mk_ctx(vol_state="CMP"), "short")
    assert top(s) == "debit_vertical"
    assert s[0]["label"] == "Put debit spread"


def test_short_nrm_inverted_front_calendar_first():
    s = gate2(mk_ctx(term_verdict="INVERTED FRONT"), "short")
    assert top(s) == "otm_calendar"
    assert s[0]["label"] == "OTM put calendar"


def test_short_nrm_skew_rich_put_debit_subsidised():
    s = gate2(mk_ctx(skew_rich=True, rr25=6.0), "short")
    assert top(s) == "debit_vertical"
    assert "subsidise" in s[0]["why"]


def test_all_cells_return_full_ranking():
    for side in ("long", "short"):
        for vs in ("CMP", "NRM", "ELV", "STR"):
            for sk in (False, True):
                for tv in ("INVERTED FRONT", "FLAT", "CONTANGO",
                           "STEEP CONTANGO"):
                    s = gate2(mk_ctx(vol_state=vs, skew_rich=sk,
                                     term_verdict=tv), side)
                    assert len(s) == 3
                    assert [x["rank"] for x in s] == [1, 2, 3]
                    assert len({x["key"] for x in s}) == 3


# ------------------------------------------------------------ vol band -----
def test_vol_band_proxy_when_no_iv_history():
    band, src = vol_band({"ivp_proxy": True, "iv30": 24.0, "har_rv": 16.0})
    assert band == "ELV" and "proxy" in src
    band, _ = vol_band({"ivp_proxy": True, "iv30": 14.0, "har_rv": 16.0})
    assert band == "CMP"
    band, _ = vol_band({"ivp_proxy": True, "iv30": 16.0, "har_rv": 16.0})
    assert band == "NRM"


def test_vol_band_rank_when_history_exists():
    band, src = vol_band({"vol_state": "ELV", "iv_pctl": 72.0})
    assert band == "ELV" and "rank" in src


# ------------------------------------------------------------ verdict ------
def test_auto_intent_resolves_side_from_bias():
    assert direction_verdict(mk_ctx(bias=2), "auto")["side"] == "long"
    assert direction_verdict(mk_ctx(bias=-1), "auto")["side"] == "short"
    d = direction_verdict(mk_ctx(bias=0), "auto")
    assert d["side"] is None and d["structures"] == []
    assert "STAND ASIDE" in d["action"]


def test_auto_intent_vol_verdict_suppresses_structures():
    d = direction_verdict(mk_ctx(vrp_fwd=5.0, bias=-2), "auto")
    assert d["play"] == "SELL_VOL"
    assert d["structures"] == [] and d["side"] is None
    assert d["action"].startswith("ACTION: SELL VOL")
    assert "Scan" in d["action"]


def test_auto_intent_delta_verdict_gives_one_structure_action():
    d = direction_verdict(mk_ctx(bias=2), "auto")
    assert d["play"] == "DELTA" and len(d["structures"]) == 3
    assert d["action"] == f"ACTION: {d['structures'][0]['label'].upper()}"


def test_vol_intent_lists_families():
    d = direction_verdict(mk_ctx(vrp_fwd=5.0), "vol")
    assert d["play"] == "SELL_VOL"
    assert "condor" in d["action"] and "Scan" in d["action"]


def test_vol_intent_no_edge_says_so():
    d = direction_verdict(mk_ctx(vrp_fwd=0.5), "vol")
    assert "NO VOL EDGE" in d["action"]


def test_delta_intent_under_vol_verdict_still_ranks_with_flag():
    d = direction_verdict(mk_ctx(vrp_fwd=5.0), "long")
    assert len(d["structures"]) == 3            # explicit view is respected
    assert d["action"].startswith("ACTION: ")
    assert "better-paid" in d["action"]         # single-line flag in the action
    assert any("VOL play" in n for n in d["notes"])


def test_delta_intent_under_delta_verdict_clean_action():
    d = direction_verdict(mk_ctx(vrp_fwd=0.5, vol_state="ELV",
                                 skew_rich=True, rr25=6.0), "long")
    assert d["action"] == "ACTION: PUT CREDIT SPREAD"


def test_hard_gates_surface_in_notes():
    ctx = mk_ctx()
    ctx.gates = [{"code": "X", "msg": "stand aside", "hard": True}]
    assert any("HARD GATE" in n for n in direction_verdict(ctx, "long")["notes"])


# ------------------------------------------------------------ API route ----
@pytest.fixture()
def client():
    from webapp import app
    return app.test_client()


def test_api_direction_mock(client):
    d = client.get("/api/direction?symbol=SPX&intent=long&mode=mock").get_json()
    assert d["symbol"] == "SPX" and d["side"] == "long"
    assert len(d["structures"]) == 3
    assert {"iv_band", "vrp_fwd", "rr25_30d", "term"} <= set(d["inputs"])


def test_api_direction_bad_intent(client):
    assert client.get("/api/direction?symbol=SPX&intent=zz").status_code == 400


def test_api_direction_unknown_symbol_mock_fails_clean(client):
    r = client.get("/api/direction?symbol=AAPL&intent=long&mode=mock")
    assert r.status_code == 502 and "error" in r.get_json()
