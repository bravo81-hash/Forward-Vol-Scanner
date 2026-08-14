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
        bars.append({"d": (date(2025, 1, 1) + timedelta(days=i)).isoformat(),
                     "o": price, "h": price + abs(wiggle), "l": price - abs(wiggle),
                     "c": price + wiggle * 0.2,
                     "v": vol * (0.4 if tighten_from is not None and i >= tighten_from else 1.0)})
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
    bars[-1]["v"] = bars[-2]["v"] * 5
    t = radar_b.trigger(m, bars)
    assert t["fired"] is True

    m.sma50_slope = -0.02
    assert radar_b.trigger(m, bars)["fired"] is False


def test_trigger_blocked_by_earnings():
    bars = make_bars(300, 100.0, low_at=200, tighten_from=240)
    m = radar_b.base_metrics("TEST", bars)
    m.sma50, m.sma50_slope, m.price = 50.0, 0.01, 55.0
    bars[-1]["v"] = bars[-2]["v"] * 5
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
    assert any("solved numerically" in r for r in s.rationale)
    assert any("Breakeven at expiry" in r for r in s.rationale)


def test_zebra_needs_long_dated_expiry():
    ctx = make_ctx(dte=30)
    ctx.slices = [ctx.slices[0]]
    assert Zebra().propose(ctx) == []


def test_extrinsic_is_zero_deep_itm():
    assert extrinsic(100.0, 10.0, 0.5, 0.30) < 0.5


# ---------------------------------------------------------------- gate E --
def test_downtrend_blocks_everything():
    ctx = make_ctx(regime={"trend": "down", "bias": -1, "adx": 30,
                           "iv30": 0.319, "rv21": 0.512, "iv_pctl": 0.39})
    out = gate_e.select(ctx)
    assert out["eligible"] is False
    assert any("TREND BLOCK" in b for b in out["blocks"])
    assert out["action"] == "STAND ASIDE"


def test_iv_below_rv_blocks_premium_selling():
    ctx = make_ctx(regime={"trend": "up", "bias": 1, "adx": 28,
                           "iv30": 0.30, "rv21": 0.50, "iv_pctl": 0.80})
    out = gate_e.select(ctx)
    assert any("PREMIUM-SELLING BLOCK" in b for b in out["blocks"])
    assert not set(out["structures"]) & gate_e.SHORT_PREMIUM
    assert any("favourably priced" in n for n in out["notes"])


def test_uptrend_cheap_vol_picks_long_premium():
    ctx = make_ctx(regime={"trend": "up", "bias": 1, "adx": 28,
                           "iv30": 0.25, "rv21": 0.24, "iv_pctl": 0.20})
    out = gate_e.select(ctx)
    assert out["eligible"] is True
    assert out["structures"][0] in gate_e.LONG_PREMIUM


def test_basing_without_trigger_is_watchlist_only():
    ctx = make_ctx(regime={"trend": "flat", "bias": 0, "adx": 15,
                           "iv30": 0.30, "rv21": 0.28, "iv_pctl": 0.40})
    out = gate_e.select(ctx, trigger_fired=False)
    assert out["eligible"] is False
    assert any("TRIGGER BLOCK" in b for b in out["blocks"])


def test_basing_with_trigger_allows_defined_risk():
    ctx = make_ctx(regime={"trend": "flat", "bias": 0, "adx": 15,
                           "iv30": 0.30, "rv21": 0.28, "iv_pctl": 0.40})
    out = gate_e.select(ctx, trigger_fired=True)
    assert out["eligible"] is True
    assert "debit_spread" in out["structures"]


def test_build_attaches_suggestions():
    ctx = make_ctx(regime={"trend": "up", "bias": 1, "adx": 28,
                           "iv30": 0.25, "rv21": 0.24, "iv_pctl": 0.20})
    out = gate_e.build(ctx, hold="long")
    assert "suggestions" in out


# ----------------------------------------------------------------- cards --
def test_blocked_card_names_gate_and_clearing_condition():
    ctx = make_ctx(regime={"trend": "down", "bias": -1, "adx": 30,
                           "iv30": 0.319, "rv21": 0.512, "iv_pctl": 0.39})
    card = cards_e.render(gate_e.build(ctx))
    flags = " ".join(card["sections"]["FLAGS"])
    assert "TREND BLOCK" in flags
    assert "PREMIUM-SELLING BLOCK" in flags
    assert card["action"] == "STAND ASIDE"


def test_card_format_rules_hold():
    ctx = make_ctx(regime={"trend": "up", "bias": 1, "adx": 28,
                           "iv30": 0.25, "rv21": 0.24, "iv_pctl": 0.20})
    card = cards_e.render(gate_e.build(ctx, hold="long"))
    assert cards_e.validate(card) == []


def test_card_text_renders():
    ctx = make_ctx(regime={"trend": "down", "bias": -1, "adx": 30,
                           "iv30": 0.319, "rv21": 0.512, "iv_pctl": 0.39})
    text = cards_e.to_text(cards_e.render(gate_e.build(ctx)))
    assert "ACTION:" in text and "FLAGS" in text
