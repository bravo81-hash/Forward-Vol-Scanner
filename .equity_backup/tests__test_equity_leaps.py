"""Tests for Radar-B, Gate E, the ZEBRA solver, fundamentals and cards."""
from __future__ import annotations

import math
from datetime import date, timedelta

import pytest

from core.fundamentals import (OK, UNRATED, Fundamentals, fundamental_gates,
                               load_history, snapshot)
from core.models import Context, Slice
from selection import cards_e, gate_e, radar_b
from strategies.zebra import Zebra, extrinsic, solve_long_strike


# ------------------------------------------------------------- fixtures --
def make_bars(n=300, start=100.0, *, drift=0.0, low_at=None, atr_pct=0.02,
              vol=2_000_000.0, tighten_from=None):
    """Deterministic synthetic bars. `low_at` forces a base low index."""
    bars, price = [], start
    for i in range(n):
        price *= (1.0 + drift)
        if low_at is not None:
            # V shape: fall to low_at, then recover slowly
            if i <= low_at:
                price = start * (1 - 0.40 * (i / max(low_at, 1)))
            else:
                frac = (i - low_at) / max(n - low_at, 1)
                price = start * 0.60 * (1 + 0.10 * frac)
        amp = atr_pct
        if tighten_from is not None and i >= tighten_from:
            amp = atr_pct * 0.25
        wiggle = amp * price * (0.5 if i % 2 else -0.5)
        bars.append({"date": (date(2025, 1, 1) + timedelta(days=i)).isoformat(),
                     "open": price, "high": price + abs(wiggle),
                     "low": price - abs(wiggle), "close": price + wiggle * 0.2,
                     "volume": vol * (0.4 if tighten_from is not None and i >= tighten_from else 1.0)})
    return bars


def make_ctx(symbol="TEST", spot=78.10, dte=305, iv=0.32, *, regime=None):
    strikes = [float(k) for k in range(40, 121, 5)]
    slc = Slice(expiry=date(2026, 8, 14) + timedelta(days=dte), dte=dte,
                atm_strike=80.0, atm_iv=iv,
                put25_iv=iv + 0.03, call25_iv=iv - 0.01,
                put25_strike=65.0, call25_strike=95.0,
                atm_spread_pct=0.02, oi_atm=2500)
    near = Slice(expiry=date(2026, 8, 14) + timedelta(days=45), dte=45,
                 atm_strike=80.0, atm_iv=iv + 0.02,
                 put25_iv=iv + 0.05, call25_iv=iv,
                 put25_strike=70.0, call25_strike=90.0,
                 atm_spread_pct=0.02, oi_atm=4000)
    return Context(symbol=symbol, spot=spot, today=date(2026, 8, 14),
                   slices=[near, slc], strikes=strikes,
                   regime=regime or {}, mode="mock")


def fund(**kw):
    base = dict(symbol="TEST", status=OK, revenue_growth=0.12, fcf=1e9,
                operating_margin=0.18, net_debt_ebitda=1.2,
                eps_trend_90d=0.03, eps_fwd_current=5.0, eps_fwd_90d=4.85)
    base.update(kw)
    return Fundamentals(**base)


# ---------------------------------------------------------- fundamentals --
def test_unrated_never_scores_neutral():
    f = Fundamentals(symbol="X", status=UNRATED, reason="fetch failed")
    passed, reasons = fundamental_gates(f)
    assert passed is False
    assert "UNRATED" in reasons[0]


def test_falling_estimates_reject_hard():
    passed, reasons = fundamental_gates(fund(eps_trend_90d=-0.08))
    assert passed is False
    assert any("deteriorating business" in r for r in reasons)


def test_rising_estimates_read_as_multiple_compression():
    passed, reasons = fundamental_gates(fund(eps_trend_90d=0.05))
    assert passed is True
    assert any("multiple compression" in r for r in reasons)


def test_leverage_gate():
    passed, _ = fundamental_gates(fund(net_debt_ebitda=5.0))
    assert passed is False


def test_no_profitability_floor_rejects():
    passed, _ = fundamental_gates(fund(fcf=-1e8, operating_margin=-0.05))
    assert passed is False


def test_snapshot_roundtrip(tmp_path):
    recs = {"AAA": fund(symbol="AAA"), "BBB": fund(symbol="BBB")}
    snapshot(recs, day=date(2026, 8, 14), directory=tmp_path)
    snapshot(recs, day=date(2026, 8, 15), directory=tmp_path)
    hist = load_history("AAA", directory=tmp_path)
    assert len(hist) == 2
    assert hist[0]["snapshot_date"] == "2026-08-14"


# --------------------------------------------------------------- radar-b --
def test_metrics_detect_drawdown_and_compression():
    bars = make_bars(300, 100.0, low_at=200, tighten_from=240)
    m = radar_b.base_metrics("TEST", bars)
    assert m.ok
    assert 0.20 < m.drawdown < 0.60
    assert m.atr_compression < 0.80


def test_shallow_drawdown_rejected():
    bars = make_bars(300, 100.0, drift=0.0005)
    m = radar_b.base_metrics("TEST", bars)
    passed, reasons = radar_b.structural_gates(m)
    assert passed is False


def test_insufficient_bars_blocks():
    m = radar_b.base_metrics("TEST", make_bars(50))
    assert m.ok is False
    passed, reasons = radar_b.structural_gates(m)
    assert passed is False
    assert "daily bars" in reasons[0]


def test_scan_returns_fewer_than_limit_when_fewer_qualify():
    cands = {"AAA": make_bars(300, 100.0, low_at=200, tighten_from=240),
             "BBB": make_bars(300, 100.0, drift=0.0005)}
    out = radar_b.scan(cands, {"AAA": fund(symbol="AAA"), "BBB": fund(symbol="BBB")})
    assert out["returned"] <= 5
    assert out["returned"] == out["qualified"]
    assert "watchlist, not a signal" in out["note"]


def test_unrated_symbol_dropped_from_watchlist():
    cands = {"AAA": make_bars(300, 100.0, low_at=200, tighten_from=240)}
    out = radar_b.scan(cands, {"AAA": Fundamentals(symbol="AAA", status=UNRATED,
                                                   reason="no estimates")})
    assert out["qualified"] == 0


def test_trigger_requires_reclaim_slope_and_volume():
    bars = make_bars(300, 100.0, low_at=200, tighten_from=240)
    m = radar_b.base_metrics("TEST", bars)
    m.sma50, m.sma50_slope, m.price = 50.0, 0.01, 55.0
    bars[-1]["volume"] = bars[-2]["volume"] * 5
    t = radar_b.trigger(m, bars)
    assert t["fired"] is True

    m.sma50_slope = -0.02
    assert radar_b.trigger(m, bars)["fired"] is False


def test_trigger_blocked_by_earnings():
    bars = make_bars(300, 100.0, low_at=200, tighten_from=240)
    m = radar_b.base_metrics("TEST", bars)
    m.sma50, m.sma50_slope, m.price = 50.0, 0.01, 55.0
    bars[-1]["volume"] = bars[-2]["volume"] * 5
    today = date(2026, 8, 14)
    t = radar_b.trigger(m, bars, earnings=today + timedelta(days=2), today=today)
    assert t["fired"] is False
    assert any("blackout" in c for c in t["checks"])


# ----------------------------------------------------------------- zebra --
def test_solver_beats_naive_seventy_delta_strike():
    """The NFLX case: solved strike must have less net extrinsic than 70."""
    ctx = make_ctx(spot=78.10, dte=305, iv=0.32)
    slc = ctx.slices[-1]
    atm = ctx.snap(ctx.spot)
    solved, diag = solve_long_strike(ctx, slc, atm)

    t = slc.dte / 365.0
    from core.chain import iv_at
    naive_net = abs(2 * extrinsic(ctx.spot, 70.0, t, iv_at(slc, 70.0)) - diag["atm_extrinsic"])
    assert diag["net_extrinsic"] < naive_net
    assert solved < 70.0


def test_zebra_delta_computed_not_assumed():
    ctx = make_ctx(spot=78.10, dte=305, iv=0.32)
    out = Zebra().propose(ctx)
    assert out, "ZEBRA should build on a normal chain"
    s = out[0]
    # greeks are published in risk-navigator units (x100)
    assert 60 <= abs(s.greeks["delta"]) <= 130
    assert any("of time value" in r for r in s.rationale)
    assert any("Net delta computed from the live chain" in r for r in s.rationale)


def test_zebra_needs_long_dated_expiry():
    ctx = make_ctx(dte=30)
    ctx.slices = [ctx.slices[0]]
    assert Zebra().propose(ctx) == []


def test_extrinsic_is_zero_deep_itm():
    assert extrinsic(100.0, 10.0, 0.5, 0.30) < 0.5


# ---------------------------------------------------------------- gate E --
def test_downtrend_blocks_everything():
    ctx = make_ctx(regime={"trend": "down", "bias": -1, "adx": 30,
                           "iv30": 32.9, "rv21": 51.2, "iv_pctl": 39.0})
    out = gate_e.select(ctx, bars=make_bars(300, 130.0, low_at=250))
    assert out["eligible"] is False
    assert any("TREND BLOCK" in b for b in out["blocks"])
    assert out["action"] == "STAND ASIDE"


def test_iv_below_rv_blocks_premium_selling():
    ctx = make_ctx(regime={"trend": "up", "bias": 1, "adx": 28,
                           "iv30": 30.0, "rv21": 50.0, "iv_pctl": 80.0})
    out = gate_e.select(ctx)
    assert any("PREMIUM-SELLING BLOCK" in b for b in out["blocks"])
    assert not set(out["structures"]) & gate_e.SHORT_PREMIUM
    assert any("favourably priced" in n for n in out["notes"])


def test_uptrend_cheap_vol_picks_long_premium():
    ctx = make_ctx(regime={"trend": "up", "bias": 1, "adx": 28,
                           "iv30": 25.0, "rv21": 24.0, "iv_pctl": 20.0})
    out = gate_e.select(ctx, bars=make_bars(300, 60.0, drift=0.0025))
    assert out["eligible"] is True
    assert out["structures"][0] in gate_e.LONG_PREMIUM


def test_basing_without_trigger_is_watchlist_only():
    ctx = make_ctx(regime={"trend": "flat", "bias": 0, "adx": 15,
                           "iv30": 30.0, "rv21": 28.0, "iv_pctl": 40.0})
    out = gate_e.select(ctx, trigger_fired=False, trend_state=0)
    assert out["eligible"] is False
    assert any("TRIGGER BLOCK" in b for b in out["blocks"])


def test_basing_with_trigger_allows_defined_risk():
    ctx = make_ctx(regime={"trend": "flat", "bias": 0, "adx": 15,
                           "iv30": 30.0, "rv21": 28.0, "iv_pctl": 40.0})
    out = gate_e.select(ctx, trigger_fired=True, trend_state=0)
    assert out["eligible"] is True
    assert "debit_spread" in out["structures"]


def test_build_attaches_suggestions():
    ctx = make_ctx(regime={"trend": "up", "bias": 1, "adx": 28,
                           "iv30": 25.0, "rv21": 24.0, "iv_pctl": 20.0})
    out = gate_e.build(ctx, hold="long", trend_state=1)
    assert "suggestions" in out


# ----------------------------------------------------------------- cards --
def test_blocked_card_names_gate_and_clearing_condition():
    ctx = make_ctx(regime={"trend": "down", "bias": -1, "adx": 30,
                           "iv30": 32.9, "rv21": 51.2, "iv_pctl": 39.0})
    card = cards_e.render(gate_e.build(ctx, bars=make_bars(300, 130.0, low_at=250)))
    flags = " ".join(card["sections"]["FLAGS"])
    assert "TREND BLOCK" in flags
    assert "PREMIUM-SELLING BLOCK" in flags
    assert card["action"] == "STAND ASIDE"


def test_card_format_rules_hold():
    ctx = make_ctx(regime={"trend": "up", "bias": 1, "adx": 28,
                           "iv30": 25.0, "rv21": 24.0, "iv_pctl": 20.0})
    card = cards_e.render(gate_e.build(ctx, hold="long", trend_state=1))
    assert cards_e.validate(card) == []


def test_card_text_renders():
    ctx = make_ctx(regime={"trend": "down", "bias": -1, "adx": 30,
                           "iv30": 32.9, "rv21": 51.2, "iv_pctl": 39.0})
    text = cards_e.to_text(cards_e.render(gate_e.build(ctx, bars=make_bars(300, 130.0, low_at=250))))
    assert "ACTION:" in text and "FLAGS" in text


# ------------------------------------------------- regressions (v2 fixes) --
def test_bar_keys_match_histories_yf():
    """histories_yf emits open/high/low/close/volume, not o/h/l/c/v."""
    bars = make_bars(300, 100.0, low_at=200, tighten_from=240)
    assert set(bars[0]) >= {"open", "high", "low", "close", "volume"}
    assert radar_b.base_metrics("TEST", bars).ok


def test_stage_ignores_short_horizon_regime_trend():
    """A multi-day bounce must not read as Stage 2 while structure is broken."""
    bars = make_bars(300, 100.0, low_at=250)          # deep decline, weak bounce
    label, code = gate_e.stage_from_bars(bars)
    assert code == -1, label

    ctx = make_ctx(regime={"trend": "UP", "bias": 2, "adx": 30,
                           "iv30": 32.9, "rv21": 51.2, "iv_pctl": 39.0})
    out = gate_e.select(ctx, bars=bars)
    assert out["eligible"] is False
    assert any("TREND BLOCK" in b for b in out["blocks"])


def test_stage_without_bars_never_claims_stage_two():
    ctx = make_ctx(regime={"trend": "UP", "bias": 2, "adx": 30,
                           "iv30": 25.0, "rv21": 24.0, "iv_pctl": 20.0})
    out = gate_e.select(ctx)
    assert out["stage_code"] == 0


def test_iv_rv_read_as_percent_not_decimal():
    """core.regime stores iv30/rv21 as percent (32.90), not decimals."""
    ctx = make_ctx(regime={"trend": "UP", "bias": 1, "adx": 28,
                           "iv30": 32.9, "rv21": 51.2, "iv_pctl": 39.0})
    out = gate_e.select(ctx)
    assert 0.6 < out["inputs"]["iv_rv"] < 0.7
    flags = " ".join(out["blocks"])
    assert "3290" not in flags and "32.9%" in flags


def test_long_hold_blocks_when_surface_has_no_leaps():
    """SCAN_DTE caps the default yf context at 85 DTE; a months hold must not
    silently return a four-week structure."""
    ctx = make_ctx(dte=45)
    ctx.slices = [ctx.slices[0]]
    bars = make_bars(300, 100.0, drift=0.001)
    out = gate_e.select(ctx, hold="long", bars=bars)
    assert any("TENOR BLOCK" in b for b in out["blocks"])
    assert out["structures"] == []


def test_action_matches_rendered_structure():
    ctx = make_ctx(regime={"trend": "UP", "bias": 1, "adx": 28,
                           "iv30": 25.0, "rv21": 24.0, "iv_pctl": 20.0})
    out = gate_e.build(ctx, hold="long", trend_state=1)
    if out["suggestions"]:
        assert out["action"] == out["suggestions"][0]["strategy"].upper()
    else:
        assert out["action"] == "STAND ASIDE"


def test_equity_context_windows():
    from selection.equity_context import window_for
    assert window_for("long")[1] > 400
    assert window_for("short")[1] < 90


# ------------------------------------------- regressions (v3, live-data) --
def test_breakeven_single_source_of_truth():
    """The closed form 2K_long - K_short + debit only holds ABOVE the short
    strike. On NVDA the true breakeven fell between the strikes and the card
    printed two different numbers for the same structure."""
    ctx = make_ctx(spot=225.30, dte=307, iv=0.217)
    ctx.strikes = [float(k) for k in range(120, 301, 5)]
    s = Zebra().propose(ctx)[0]
    assert not [r for r in s.rationale if "Breakeven at expiry" in r], \
        "the card renderer states breakeven; the rationale must not repeat it"
    assert s.breakevens, "struct_metrics must supply a breakeven"
    # the closed form is only valid above the short strike and must not be used
    closed_form = 2 * 195.0 - 225.0 + s.net_mid
    assert abs(s.breakevens[0] - closed_form) > 0.01 or s.breakevens[0] > 225.0


def test_solver_reports_real_long_extrinsic_not_half_of_atm():
    ctx = make_ctx(spot=225.30, dte=307, iv=0.217)
    ctx.strikes = [float(k) for k in range(120, 301, 5)]
    atm = ctx.snap(ctx.spot)
    _, diag = solve_long_strike(ctx, ctx.slices[-1], atm)
    assert diag["long_extrinsic"] != pytest.approx(diag["atm_extrinsic"] / 2, rel=1e-9) \
        or diag["net_extrinsic"] == pytest.approx(0.0, abs=1e-6)


def test_thin_strike_grid_is_declared():
    """Three surface points is not a solvable grid; the card must say so."""
    ctx = make_ctx(spot=225.30, dte=307, iv=0.217)
    ctx.strikes = [212.0, 225.0, 240.0]
    s = Zebra().propose(ctx)[0]
    assert any("thin grid" in r for r in s.rationale)


def test_degraded_solve_is_not_called_a_zebra():
    ctx = make_ctx(spot=225.30, dte=307, iv=0.217)
    ctx.strikes = [212.0, 225.0]
    s = Zebra().propose(ctx)[0]
    joined = " ".join(s.rationale)
    assert "back ratio carrying real premium" in joined


def test_proxy_iv_rank_never_shown_as_measured():
    ctx = make_ctx(regime={"trend": "UP", "bias": 1, "adx": 28, "iv30": 21.7,
                           "rv21": 39.7, "iv_pctl": 50.0, "ivp_proxy": True})
    out = gate_e.select(ctx, bars=make_bars(300, 60.0, drift=0.0025))
    assert out["inputs"]["iv_rank"] is None
    assert out["inputs"]["iv_rank_proxy"] is True
    card = cards_e.render(out)
    assert "IV rank 50" not in card["header"]["meta"]
    assert any("no free implied-volatility history" in n for n in out["notes"])


# ----------------------------------------- regressions (v4, yfinance flake) --
def test_retry_treats_empty_response_as_failure():
    from selection import equity_context as ec
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        return [] if calls["n"] < 3 else ["2027-06-18"]

    ec.BACKOFF = 0.001
    assert ec._retry(flaky, "test") == ["2027-06-18"]
    assert calls["n"] == 3


def test_retry_raises_with_diagnosis_after_exhaustion():
    from selection import equity_context as ec
    ec.BACKOFF = 0.001
    with pytest.raises(RuntimeError, match="empty response"):
        ec._retry(lambda: [], "throttled")


def test_cache_avoids_refetch():
    from selection import equity_context as ec
    ec.clear_cache()
    calls = {"n": 0}

    def fetch():
        calls["n"] += 1
        return ["x"]

    assert ec._cached(("k",), fetch) == ["x"]
    assert ec._cached(("k",), fetch) == ["x"]
    assert calls["n"] == 1
    ec.clear_cache()


def test_tenor_shortfall_blocks_with_real_numbers():
    """A name with no LEAPS listed must say so, not report a data failure."""
    ctx = make_ctx(dte=45)
    ctx.slices = [ctx.slices[0]]
    ctx.data = {"tenor_shortfall": {"requested": [90, 500],
                                    "available_dte": [7, 66],
                                    "count_in_window": 0}}
    out = gate_e.select(ctx, hold="long", bars=make_bars(300, 100.0, drift=0.001))
    joined = " ".join(out["blocks"])
    assert "TENOR BLOCK" in joined
    assert "7 to 66 days" in joined
    assert "listings fact rather than a data failure" in joined
    assert out["structures"] == []


# ------------------------------------ regressions (v5, chain-call budget) --
def test_dte_range_is_bracketed_to_few_chains(monkeypatch):
    """build_context_yf fetches one chain per expiry in its range. A wide
    LEAPS window asked for 16 and got throttled into empty responses, which
    then reported as '0 expiries' - a message about the wrong thing."""
    from selection import equity_context as ec
    ec.clear_cache()

    today = date(2026, 8, 14)
    listed = [(f"2026-{m:02d}-18", d) for m, d in
              ((9, 35), (10, 66), (12, 126), (1, 157), (3, 216), (6, 307), (9, 400))]
    monkeypatch.setattr(ec, "probe_expiries", lambda s, t: listed)

    seen = {}

    def fake_ctx(symbol, today=None, dte_range=None):
        seen["range"] = dte_range
        seen["count"] = sum(1 for _, d in listed
                            if dte_range[0] <= d <= dte_range[1])
        return make_ctx(symbol)

    monkeypatch.setattr("core.yf_client.build_context_yf", fake_ctx)
    monkeypatch.setattr("core.stock_data.histories_yf", lambda s, period="2y": {})
    monkeypatch.setattr(ec, "listed_strikes", lambda *a, **k: [])

    ctx, _ = ec.build("TEST", "long", today=today)
    assert seen["count"] <= ec.MAX_SLICES, seen
    assert ctx.data["chains_fetched"] <= ec.MAX_SLICES
    # and the bracket must sit around the 300-day target, not span the window
    assert seen["range"][0] > 150, seen["range"]
    ec.clear_cache()


def test_context_is_cached_across_repeat_analyses(monkeypatch):
    from selection import equity_context as ec
    ec.clear_cache()
    today = date(2026, 8, 14)
    monkeypatch.setattr(ec, "probe_expiries", lambda s, t: [("2027-06-18", 307),
                                                            ("2027-03-19", 216),
                                                            ("2026-12-18", 126)])
    calls = {"n": 0}

    def fake_ctx(symbol, today=None, dte_range=None):
        calls["n"] += 1
        return make_ctx(symbol)

    monkeypatch.setattr("core.yf_client.build_context_yf", fake_ctx)
    monkeypatch.setattr("core.stock_data.histories_yf", lambda s, period="2y": {})
    monkeypatch.setattr(ec, "listed_strikes", lambda *a, **k: [])

    ec.build("TEST", "long", today=today)
    ec.build("TEST", "long", today=today)
    assert calls["n"] == 1
    ec.clear_cache()


# --------------------------------- regressions (v6, Yahoo placeholder IV) --
def test_iv_solver_round_trips():
    from core.iv_solve import implied_vol
    from core.pricing import bs_price
    for iv in (0.18, 0.32, 0.55, 0.95):
        for k in (140.0, 195.0, 225.0, 300.0):
            px = bs_price(225.30, k, 307 / 365, iv, "C")
            assert implied_vol(px, 225.30, k, 307 / 365, "C") == pytest.approx(iv, abs=2e-3)


def test_iv_solver_handles_deep_itm_where_newton_would_diverge():
    """A ZEBRA long leg lives deep ITM, where vega collapses."""
    from core.iv_solve import implied_vol
    from core.pricing import bs_price
    px = bs_price(225.30, 60.0, 307 / 365, 0.30, "C")
    assert implied_vol(px, 225.30, 60.0, 307 / 365, "C") is not None


def test_iv_solver_rejects_impossible_prices():
    from core.iv_solve import implied_vol
    assert implied_vol(0.0, 225.0, 200.0, 0.5, "C") is None
    assert implied_vol(1.0, 225.0, 200.0, 0.5, "C") is None      # below intrinsic
    assert implied_vol(999.0, 225.0, 200.0, 0.5, "C") is None    # above bound


def test_yahoo_placeholder_iv_is_detected():
    """The exact values NVDA returned on every LEAPS expiry."""
    from core.iv_solve import is_sane
    assert not is_sane(1.0000000000000003e-05)
    assert not is_sane(0.0004982763671875)
    assert not is_sane(None)
    assert is_sane(0.317)


def test_repair_iv_replaces_placeholders_but_keeps_real_quotes():
    pd = pytest.importorskip("pandas")
    from core.iv_solve import repair_iv
    from core.pricing import bs_price

    spot, t = 225.30, 307 / 365
    fair = bs_price(spot, 225.0, t, 0.32, "C")
    frame = pd.DataFrame([
        {"strike": 225.0, "bid": fair - 0.2, "ask": fair + 0.2,
         "lastPrice": fair, "impliedVolatility": 1e-05},          # placeholder
        {"strike": 200.0, "bid": 40.0, "ask": 41.0,
         "lastPrice": 40.5, "impliedVolatility": 0.285},          # real
    ])
    out = repair_iv(frame, spot, t, "C")
    assert out.loc[0, "impliedVolatility"] == pytest.approx(0.32, abs=5e-3)
    assert out.loc[1, "impliedVolatility"] == 0.285


# ------------------------------------------- regressions (v7, card dupes) --
def test_card_states_breakeven_exactly_once():
    """It appeared twice on the NVDA card: once from the renderer, once from
    the ZEBRA rationale."""
    ctx = make_ctx(spot=225.30, dte=307, iv=0.408,
                   regime={"trend": "UP", "bias": 1, "adx": 28, "iv30": 40.8,
                           "rv21": 39.7, "iv_pctl": 50.0, "ivp_proxy": True})
    ctx.strikes = [float(k) for k in range(120, 301, 5)]
    card = cards_e.render(gate_e.build(ctx, "long", trend_state=1,
                                       bars=make_bars(300, 60.0, drift=0.0025)))
    bullets = card["sections"].get("STRUCTURE", [])
    assert sum(1 for b in bullets if "Breakeven at expiry" in b) <= 1, bullets


def test_solver_prefers_real_curve_over_three_anchor_interpolation():
    ctx = make_ctx(spot=225.30, dte=307, iv=0.408)
    ctx.strikes = [float(k) for k in range(120, 301, 5)]
    slc = ctx.slices[-1]
    from strategies.zebra import curve_iv
    assert curve_iv(ctx, slc, 210.0) == pytest.approx(iv_at_ref(slc, 210.0), abs=1e-9)

    ctx.data["iv_curve"] = {slc.expiry.isoformat(): {210.0: 0.372}}
    assert curve_iv(ctx, slc, 210.0) == 0.372


def iv_at_ref(slc, k):
    from core.chain import iv_at
    return iv_at(slc, k)


def test_degraded_solve_reports_what_it_evaluated():
    ctx = make_ctx(spot=225.30, dte=307, iv=0.408)
    ctx.strikes = [210.0, 225.0]
    s = Zebra().propose(ctx)[0]
    assert any("Closest strikes the solver could reach" in r for r in s.rationale)
