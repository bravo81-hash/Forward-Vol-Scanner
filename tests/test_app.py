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


def test_hard_gate_warns_not_blocks():
    """A hard gate (IV spike) is now the strongest WARNING, not a block:
    suggestions still come back and the verdict carries the warning."""
    from core.context import build_context
    from selection.ranker import family_priority
    ctx = build_context("SPX", "mock", TODAY)
    ctx.regime["iv_chg_pct"] = 20.0
    from core.regime import build_gates
    ctx.gates = build_gates(ctx.regime, ctx.events)
    assert any(g["hard"] for g in ctx.gates)
    fams, verdict = family_priority(ctx)
    assert fams                                   # cards still shown
    assert verdict.startswith("WARNING")
    assert "unsettled" in verdict                 # the IV-spike reason surfaces


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
    book = {"greeks": {"delta": 0.0, "gamma": 0, "theta": 100.0, "vega": 1100.0}}
    long_vega = Suggestion("calendar", "x", [], 1, {"delta": 0, "gamma": 0,
                           "theta": 50.0, "vega": 300.0}, 1, -1, [], 0, [])
    short_vega = Suggestion("condor", "x", [], -1, {"delta": 0, "gamma": 0,
                            "theta": 50.0, "vega": -300.0}, 1, -1, [], 0, [])
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


# ------------------------------------------------ FOMC event harvest --------

def _harvest_ctx(vrp=-0.4):
    """SPX mock ctx 10d before FOMC 2026-07-29, forced into the harvest gate."""
    from core.context import build_context
    from core.regime import build_gates
    ctx = build_context("SPX", "mock", date(2026, 7, 19))
    ctx.regime.update(vrp=vrp, rv21=10.0, iv_chg_pct=0.0,
                      vol_state="NRM", gamma="+g")
    ctx.regime["term"]["verdict"] = "INVERTED FRONT"
    ctx.gates = build_gates(ctx.regime, ctx.events, ctx.today)
    return ctx


def test_event_harvest_fires():
    from selection.ranker import family_priority, shortlist
    fams, verdict = family_priority(_harvest_ctx())
    assert any(why.startswith("FOMC harvest") for _, why in fams)
    assert "event harvest" in verdict
    sl = shortlist(_harvest_ctx())
    evs = [c for c in sl["cards"] if c["label"].startswith("EVENT CAL")]
    assert evs
    fomc = date(2026, 7, 29)
    for c in evs:
        front = min(date.fromisoformat(l["expiry"]) for l in c["legs_raw"])
        assert front >= fomc
        assert any("DO NOT apply" in r for r in c["rationale"])


def test_event_harvest_not_offered_when_thin(monkeypatch):
    """Thin event premium: no EVENT CAL family is offered, but the app still
    shows best-available suggestions rather than standing aside."""
    import core.surface as cs
    from selection.ranker import family_priority
    monkeypatch.setattr(cs, "FOMC_HIST_MOVE_PCT", 5.0)
    fams, verdict = family_priority(_harvest_ctx())
    assert not any(why.startswith("FOMC harvest") for _, why in fams)
    assert fams                                   # suggestions still shown
    assert not verdict.startswith("TRADE")        # but flagged cautionary


def test_event_harvest_not_offered_deep_vrp():
    from selection.ranker import family_priority
    fams, verdict = family_priority(_harvest_ctx(vrp=-3.0))
    assert not any(why.startswith("FOMC harvest") for _, why in fams)
    assert fams
    assert not verdict.startswith("TRADE")


# ------------------------------------------------ Friday cadence gate -------

def _condor_regime_ctx(day):
    from core.context import build_context
    from core.regime import build_gates
    ctx = build_context("SPX", "mock", day)
    ctx.regime.update(vrp=1.5, vol_state="NRM", trend="RNG", gamma="+g",
                      iv_chg_pct=0.0)
    ctx.regime["term"]["verdict"] = "FLAT"
    ctx.gates = build_gates(ctx.regime, ctx.events, ctx.today)
    return ctx


def test_friday_warns_not_blocks_condor():
    """Friday no longer removes credit families — the W gate warns and the
    debit structures merely rank first; condor is still offered."""
    from selection.ranker import family_priority
    ctx = _condor_regime_ctx(date(2026, 6, 26))          # a Friday
    assert any(g["code"] == "W" for g in ctx.gates)      # Friday warning present
    fams, _ = family_priority(ctx)
    assert fams                                          # suggestions still shown
    assert any(k == "condor" for k, _ in fams)           # condor not removed


def test_monday_allows_condor():
    from selection.ranker import family_priority
    ctx = _condor_regime_ctx(date(2026, 6, 22))          # the Monday before
    assert not any(g["code"] == "W" for g in ctx.gates)
    fams, _ = family_priority(ctx)
    assert any(k == "condor" for k, _ in fams)


# ------------------------------------------------ campaign scope + stress ---

def test_campaign_legs_excluded():
    from datetime import timedelta
    from core.context import build_context
    from portfolio.book import book_greeks
    ctx = build_context("SPX", "mock", TODAY)
    near = (TODAY + timedelta(days=30)).strftime("%Y%m%d")
    far = (TODAY + timedelta(days=160)).strftime("%Y%m%d")
    pos = [{"cp": "P", "strike": 6000.0, "expiry": near, "qty": -1, "conId": 1},
           {"cp": "P", "strike": 5800.0, "expiry": far, "qty": 2, "conId": 2}]
    b = book_greeks(ctx, pos)
    assert b["excluded_long_dte"] == 1
    assert b["positions"] == 2
    assert b["min_short_dte"] == 30
    assert b["greeks"] == book_greeks(ctx, pos[:1])["greeks"]


# ------------------------------------------------ NY timezone anchor ---------

def test_trading_today_tz(monkeypatch):
    """A Melbourne Saturday morning (07:00 AEST) is still Friday in New York."""
    from datetime import date as D
    from zoneinfo import ZoneInfo
    import datetime as _dt
    import core.events as ce

    # 2026-06-27 07:00 AEST  ==  2026-06-26 21:00 UTC  ==  2026-06-26 17:00 EDT
    mel_tz = ZoneInfo("Australia/Melbourne")
    aware_mel = _dt.datetime(2026, 6, 27, 7, 0, tzinfo=mel_tz)

    class _FakeDT:
        @staticmethod
        def now(tz=None):
            return aware_mel.astimezone(tz) if tz else aware_mel

    monkeypatch.setattr(ce, "datetime", _FakeDT)
    assert ce.trading_today() == D(2026, 6, 26)   # Friday in New York


def test_friday_gate_ny_anchored():
    """build_gates fires W on NY Friday, silent on NY Thursday."""
    from core.regime import build_gates
    regime = {"vrp": 1.0, "iv_chg_pct": 0.0, "vol_state": "NRM",
              "gamma": "+g", "term": {"verdict": "FLAT"}, "trend": "RNG",
              "adx": 20, "iv_pctl": 40, "rv21": 12, "bias": 0}
    events = {"fomc_dte": 30, "fomc_in_front": False,
              "opex_week": False, "post_opex": False, "ex_div": False}
    gates_fri = build_gates(regime, events, date(2026, 6, 26))   # Friday
    assert any(g["code"] == "W" for g in gates_fri)
    gates_thu = build_gates(regime, events, date(2026, 6, 25))   # Thursday
    assert not any(g["code"] == "W" for g in gates_thu)


def test_stress_book_direction():
    from datetime import timedelta
    from core.context import build_context
    from portfolio.book import stress_book
    ctx = build_context("SPX", "mock", TODAY)
    exp = (TODAY + timedelta(days=30)).strftime("%Y%m%d")
    short_puts = [{"cp": "P", "strike": 6000.0, "expiry": exp, "qty": -2, "conId": 1}]
    long_puts = [{"cp": "P", "strike": 6000.0, "expiry": exp, "qty": 2, "conId": 1}]
    s_short = stress_book(ctx, short_puts)
    assert s_short[0]["name"].startswith("-5%")
    assert s_short[0]["pnl"] < 0
    assert stress_book(ctx, long_puts)[0]["pnl"] > 0
