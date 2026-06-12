"""Mock-mode test suite — runs with zero TWS dependency."""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

TODAY = date(2026, 6, 15)


def test_pricing_parity():
    from core.pricing import bs_price
    c = bs_price(6000, 6000, 30 / 365, .15, "C")
    p = bs_price(6000, 6000, 30 / 365, .15, "P")
    import math
    assert abs((c - p) - (6000 - 6000 * math.exp(-.04 * 30 / 365))) < 1e-6


def test_calendar_metrics_shape():
    from datetime import timedelta
    from core.models import Leg
    from core.pricing import struct_metrics
    legs = [Leg("P", 6000, TODAY + timedelta(days=14), -1, .15),
            Leg("P", 6000, TODAY + timedelta(days=35), 1, .145)]
    m = struct_metrics(6000, legs, TODAY)
    assert m["entry"] > 0 and m["max_profit"] > 0 and m["max_loss"] < 0
    assert len(m["breakevens"]) == 2


def test_chain_and_pairs():
    from core.chain import build_chain_mock
    from core.surface import pair_table
    spot, sl, ks = build_chain_mock("SPX", TODAY)
    assert len(sl) >= 4 and spot == 6000
    pt = pair_table(sl, TODAY)
    for p in pt:                       # FOMC-in-back-only pairs must be gone
        from datetime import date as D
        from core.events import fomc_between
        assert not fomc_between(D.fromisoformat(p["front"]), D.fromisoformat(p["back"]))


def test_shortlist_all_symbols():
    from core.context import build_context
    from selection.ranker import shortlist
    for sym in ("SPX", "SPY", "QQQ", "RUT", "IWM"):
        sl = shortlist(build_context(sym, "mock", TODAY))
        assert sl["verdict"]
        assert len(sl["cards"]) <= 4
        for c in sl["cards"]:
            assert c["max_loss"] <= 0 and c["legs_raw"]


def test_hard_gate_blocks():
    from core.context import build_context
    from selection.ranker import family_priority
    ctx = build_context("SPX", "mock", TODAY)
    ctx.regime["iv_chg_pct"] = 20.0
    from core.regime import build_gates
    ctx.gates = build_gates(ctx.regime, ctx.events)
    fams, verdict = family_priority(ctx)
    assert fams == [] and verdict.startswith("STAND ASIDE")


def test_front_exit_rule():
    """No single-expiry suggestion may leave a short leg under 7 DTE at exit."""
    from core.context import build_context
    from strategies import REGISTRY
    ctx = build_context("SPX", "mock", TODAY)
    for key in ("condor", "bwb"):
        for s in REGISTRY[key].propose(ctx):
            front = min((l.expiry - TODAY).days for l in s.legs)
            assert front - 10 >= 7, f"{key} violates front-exit rule"


def test_fit_score_direction():
    from core.models import Suggestion
    from portfolio.risk import fit_score
    book = {"greeks": {"delta": 0.0, "gamma": 0, "theta": 1.0, "vega": 11.0}}
    long_vega = Suggestion("calendar", "x", [], 1, {"delta": 0, "gamma": 0,
                           "theta": .5, "vega": 3.0}, 1, -1, [], 0, [])
    short_vega = Suggestion("condor", "x", [], -1, {"delta": 0, "gamma": 0,
                            "theta": .5, "vega": -3.0}, 1, -1, [], 0, [])
    assert fit_score(book, short_vega) > fit_score(book, long_vega)


def test_web_endpoints_mock():
    import webapp
    cl = webapp.app.test_client()
    d = cl.get("/api/suggest?symbol=QQQ&mode=mock").get_json()
    assert "cards" in d and d["mode"] == "mock"
    if d["cards"]:
        c = d["cards"][0]
        p = cl.post("/api/payoff", json={"spot": d["spot"], "legs": c["legs_raw"]}).get_json()
        assert len(p["x"]) == len(p["expiry"]) > 20
        s = cl.post("/api/stage", json={"symbol": "QQQ", "mode": "mock",
                                        "legs": c["legs_raw"], "net_mid": c["net_mid"]}).get_json()
        assert s["status"] == "MockStaged"


def test_budget_scaling():
    from portfolio.risk import budget_for
    assert budget_for(400_000)["vega"] == 4 * budget_for(100_000)["vega"]
    assert budget_for(None)["vega"] == budget_for(100_000)["vega"]


def test_accounts_endpoint_mock():
    import webapp
    cl = webapp.app.test_client()
    a = cl.get("/api/accounts?mode=mock").get_json()
    assert a and a[0]["account"] == "MOCK-A"


def test_target_flags_emitted():
    """Off-band delta and failed gamma-breakeven must both emit TARGET flags."""
    from core.context import build_context
    from core.models import Suggestion
    from strategies import REGISTRY
    ctx = build_context("SPX", "mock", TODAY)
    cal = REGISTRY["calendar"]
    s = Suggestion("calendar", "x", [], 10.0,
                   {"delta": 0.30, "gamma": -0.05, "theta": 0.10, "vega": 2.0},
                   1, -1, [], 1.0, [])
    cal._check_targets(ctx, s, cal.delta_band, True)
    flags = [r for r in s.rationale if r.startswith("TARGET:")]
    assert len(flags) == 2          # delta out of band AND gamma test fails
    assert s.score < 1.0            # penalised


def test_targets_clean_pass():
    from core.context import build_context
    from core.models import Suggestion
    from strategies import REGISTRY
    ctx = build_context("SPX", "mock", TODAY)
    cal = REGISTRY["calendar"]
    s = Suggestion("calendar", "x", [], 10.0,
                   {"delta": 0.02, "gamma": -0.0005, "theta": 1.0, "vega": 2.5},
                   1, -1, [], 1.0, [])
    cal._check_targets(ctx, s, cal.delta_band, True)
    assert not any(r.startswith("TARGET:") for r in s.rationale)
