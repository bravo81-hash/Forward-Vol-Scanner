"""F1-F4 + P1-P3 workflow-package tests. Zero TWS. Independent references."""
import math
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.context import build_context
from core.models import Context, Leg, Suggestion
from selection.ranker import shortlist, verdict_size
from selection.manage import management_plan, _p_touch
from portfolio.risk import lots_for, budget_for
from portfolio.book import book_greeks

TODAY = date(2026, 7, 1)


def _ctx(**over):
    ctx = build_context("SPX", "mock", TODAY)
    for k, v in over.items():
        setattr(ctx, k, v)
    return ctx


# ---------------------------------------------------- P1 sizing -------------
def test_lots_respect_vega_budget():
    g = {"vega": 6.0, "delta": 0.02, "gamma": 0.0, "theta": 0.0}
    r = lots_for(g, 200_000, "FULL")           # vega budget = 24 -> 4 lots
    assert r["lots"] == 4 and r["binding"] == "vega"


def test_lots_scale_with_size_fraction():
    g = {"vega": 6.0, "delta": 0.02}
    assert lots_for(g, 200_000, "FULL")["lots"] == 4
    assert lots_for(g, 200_000, "HALF")["lots"] == 2
    assert lots_for(g, 200_000, "QUARTER")["lots"] == 1


def test_lots_subtract_existing_book():
    g = {"vega": 6.0, "delta": 0.02}
    book = {"greeks": {"vega": 12.0, "delta": 0.0, "gamma": 0, "theta": 0}, "nlv": 200_000}
    # budget 24, already 12 used -> 12 headroom -> 2 lots
    assert lots_for(g, 200_000, "FULL", book)["lots"] == 2


def test_lots_flat_structure_reports_margin_basis():
    r = lots_for({"vega": 0.0, "delta": 0.0}, 100_000, "FULL")
    assert r["lots"] == 0 and r["binding"] is None


# ---------------------------------------------------- F4 size ---------------
def test_verdict_size_mapping():
    assert verdict_size("TRADE", {"gamma": "+g", "vol_state": "NRM"}, []) == "FULL"
    assert verdict_size("TRADE — CAUTION (half size)", {"gamma": "+g"}, []) == "HALF"
    assert verdict_size("x quarter size", {}, []) == "QUARTER"
    assert verdict_size("WARNING — stressed", {}, []) == "STAND"
    assert verdict_size("TRADE", {"gamma": "-g", "vol_state": "NRM"}, []) == "HALF"


# ---------------------------------------------------- P2 management ---------
def test_p_touch_reflection_reference():
    # 1 EM away over the horizon -> ~2*(1-N(1)) ~= 31.7%
    spot, days, rv = 6000.0, 21, 20.0
    sigma = rv / 100 * math.sqrt(days / 252)
    k = spot * math.exp(sigma)                  # exactly 1 sigma up
    ref = 2 * (1 - 0.5 * (1 + math.erf(1 / math.sqrt(2)))) * 100
    assert abs(_p_touch(spot, k, rv, days) - ref) < 0.5


def test_management_plan_credit_structure():
    ctx = _ctx()
    legs = [Leg("P", 5800, TODAY + timedelta(days=30), -1, .16),
            Leg("P", 5750, TODAY + timedelta(days=30), 1, .17),
            Leg("C", 6200, TODAY + timedelta(days=30), -1, .15),
            Leg("C", 6250, TODAY + timedelta(days=30), 1, .155)]
    s = Suggestion("condor", "IC", legs, net_mid=-2.0, greeks={"delta": 0, "gamma": 0,
                   "theta": 0.1, "vega": -3}, max_profit=2.0, max_loss=-3.0,
                   breakevens=[5798, 6202], score=1.0, rationale=[])
    m = management_plan(ctx, s)
    assert m["basis"] == "credit"
    assert m["pt_dollars"] == 100 and m["sl_dollars"] == -300   # 0.5*2*100 ; max(-400,-300 floor)
    assert len(m["triggers"]) == 2 and all(0 <= t["p_touch_pct"] <= 100 for t in m["triggers"])
    assert m["time_stop"]["exit_in_days"] == 30 - 7


def test_management_plan_debit_calendar():
    ctx = _ctx()
    legs = [Leg("C", 6000, TODAY + timedelta(days=14), -1, .15),
            Leg("C", 6000, TODAY + timedelta(days=35), 1, .145)]
    s = Suggestion("calendar", "Cal", legs, net_mid=3.0, greeks={"delta": 0, "gamma": -0.001,
                   "theta": 0.2, "vega": 4}, max_profit=5.0, max_loss=-3.0,
                   breakevens=[5950, 6050], score=1.0, rationale=[])
    m = management_plan(ctx, s)
    assert m["basis"] == "debit"
    assert m["pt_dollars"] == 150 and m["sl_dollars"] == -150     # +50% / -50% of 3.0


# ---------------------------------------------------- F1 mandate ------------
def test_mandate_blocks_multi_expiry_on_smsf_spx():
    ctx = _ctx()
    ctx.mandate = {"account": "U-SMSF", "investing": True, "block_multi_expiry": True}
    # force a calm+range regime so calendar would otherwise lead
    ctx.regime.update({"vol_state": "CMP", "trend": "RNG", "gamma": "+g", "vrp": 1.0})
    ctx.regime["term"] = {"verdict": "CONTANGO"}
    out = shortlist(ctx)
    fams = {c["strategy"] for c in out["cards"]}
    assert not (fams & {"calendar", "double_calendar", "diagonal"})
    assert "blocked" in out["verdict"] or "single-expiry" in out["verdict"].lower()


def test_mandate_absent_allows_calendars():
    ctx = _ctx()
    ctx.regime.update({"vol_state": "CMP", "trend": "RNG", "gamma": "+g", "vrp": 1.0})
    ctx.regime["term"] = {"verdict": "CONTANGO"}
    out = shortlist(ctx)
    assert any(c["strategy"] in ("calendar", "double_calendar") for c in out["cards"])


# ---------------------------------------------------- F2 greeks source ------
def test_book_greeks_prefers_tws_when_present():
    ctx = _ctx()
    exp = (TODAY + timedelta(days=30)).strftime("%Y%m%d")
    tws = {"delta": 0.10, "gamma": 0.001, "theta": -0.05, "vega": 0.20}
    pos = [{"cp": "C", "strike": 6000, "expiry": exp, "qty": 2, "conId": 1, "greeks": tws}]
    bg = book_greeks(ctx, pos)
    assert bg["greeks_source"] == "tws"
    assert abs(bg["greeks"]["delta"] - 0.20) < 1e-9      # 0.10 * qty 2


def test_book_greeks_falls_back_to_model():
    ctx = _ctx()
    exp = (TODAY + timedelta(days=30)).strftime("%Y%m%d")
    pos = [{"cp": "C", "strike": 6000, "expiry": exp, "qty": 1, "conId": 1}]  # no greeks
    assert book_greeks(ctx, pos)["greeks_source"] == "model"


# ---------------------------------------------------- F3 freshness ----------
def test_mock_context_is_fresh():
    assert build_context("SPX", "mock", TODAY).data["fresh"] is True


def test_freshness_flags_stale_bars():
    from core.context import _freshness
    stale = _freshness([(date(2026, 6, 1), 0, 0, 0, 0)], date(2026, 7, 1), "live")
    fresh = _freshness([(date(2026, 6, 30), 0, 0, 0, 0)], date(2026, 7, 1), "live")
    assert stale["fresh"] is False and "STALE" in stale["note"]
    assert fresh["fresh"] is True


# ---------------------------------------------------- P1+P2 on real cards ---
def test_shortlist_attaches_lots_and_manage():
    ctx = _ctx()
    ctx.book = {"nlv": 250_000, "greeks": {"vega": 0, "delta": 0, "gamma": 0, "theta": 0}}
    out = shortlist(ctx)
    assert out["cards"], "no cards produced"
    c = out["cards"][0]
    assert "lots" in c and "size" in c["lots"]
    assert "manage" in c and "pt_dollars" in c["manage"] and "triggers" in c["manage"]
    assert out["size"] in ("FULL", "HALF", "QUARTER", "STAND")
