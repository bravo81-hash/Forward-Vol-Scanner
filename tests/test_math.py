"""Accuracy verification against implementation-independent references.

Run: pytest test_math.py -q     (zero TWS; pure math)

References used — none reuse the production code path:
  * put-call parity identity (with dividends)
  * central finite differences for every greek
  * GBM simulation with known sigma for realized vol
  * textbook Wilder ADX written independently
  * hand-computed variance-time interpolation and forward vol
"""
import math
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

TODAY = date(2026, 6, 15)
R, Q = 0.04, 0.013


def _p(s, k, t, iv, cp, q=Q):
    from core.pricing import bs_price
    return bs_price(s, k, t, iv, cp, r=R, q=q)


# ------------------------------------------------------------- pricing ------
def test_parity_with_dividends():
    c, p = _p(6000, 6100, 45 / 365, .16, "C"), _p(6000, 6100, 45 / 365, .16, "P")
    rhs = 6000 * math.exp(-Q * 45 / 365) - 6100 * math.exp(-R * 45 / 365)
    assert abs((c - p) - rhs) < 1e-8


def test_parity_without_dividends_unchanged():
    c, p = _p(6000, 6000, 30 / 365, .15, "C", q=0.0), _p(6000, 6000, 30 / 365, .15, "P", q=0.0)
    assert abs((c - p) - (6000 - 6000 * math.exp(-R * 30 / 365))) < 1e-8


def test_greeks_match_finite_differences():
    from core.pricing import bs_greeks
    s, k, t, iv = 6000., 6050., 30 / 365, .15
    for cp in ("C", "P"):
        g = bs_greeks(s, k, t, iv, cp, r=R, q=Q)
        h = .01
        fd_delta = (_p(s + h, k, t, iv, cp) - _p(s - h, k, t, iv, cp)) / (2 * h)
        assert abs(g["delta"] - fd_delta) < 1e-5
        fd_gamma = (_p(s + h, k, t, iv, cp) - 2 * _p(s, k, t, iv, cp)
                    + _p(s - h, k, t, iv, cp)) / h ** 2
        assert abs(g["gamma"] - fd_gamma) < 1e-4
        dv = 1e-5
        fd_vega = (_p(s, k, t, iv + dv, cp) - _p(s, k, t, iv - dv, cp)) / (2 * dv) / 100
        assert abs(g["vega"] - fd_vega) < 1e-6
        dt = 1e-6
        fd_theta = -(_p(s, k, t + dt, iv, cp) - _p(s, k, t - dt, iv, cp)) / (2 * dt) / 365
        assert abs(g["theta"] - fd_theta) < 1e-6


# --------------------------------------------------------------- regime -----
def test_realized_vol_recovers_gbm_sigma():
    from core.regime import _rv
    rnd = random.Random(11)
    sig = 0.20 / math.sqrt(252)
    cs = [100.0]
    for _ in range(4000):
        cs.append(cs[-1] * math.exp(rnd.gauss(0, sig)))
    assert abs(_rv(cs, 3999) - 20.0) < 1.0


def test_true_range_includes_gap_side():
    from core.regime import _tr
    assert _tr(5865.0, 5820.0, 6000.0) == 180.0     # -3% gap: |low - prevC|
    assert _tr(101.0, 99.0, 100.0) == 2.0           # inside day: high - low
    assert _tr(103.0, 101.0, 100.0) == 3.0          # gap up: |high - prevC|


def _ref_wilder_atr(hs, ls, cs, n=14):
    """Textbook Wilder ATR (RMA of TR), written independently — same shape
    as _ref_wilder_adx below, kept separate since ATR has no DI/DX step."""
    trs = [max(hs[i] - ls[i], abs(hs[i] - cs[i - 1]), abs(ls[i] - cs[i - 1]))
           for i in range(1, len(cs))]
    a = sum(trs[:n]) / n
    for tr in trs[n:]:
        a = (a * (n - 1) + tr) / n
    return a


def test_atr_matches_wilder_reference():
    from core.regime import _atr, mock_bars
    for sym, spot in (("SPX", 6000.), ("QQQ", 525.), ("RUT", 2300.)):
        b = mock_bars(sym, spot, TODAY, n=300)
        hs, ls, cs = [x[2] for x in b], [x[3] for x in b], [x[4] for x in b]
        window = hs[-120:], ls[-120:], cs[-120:]
        assert abs(_atr(*window) - _ref_wilder_atr(*window)) < 1e-9


def _ref_wilder_adx(hs, ls, cs, n=14):
    """Textbook Wilder ADX (RMA of DX), written independently."""
    trs, pd_, nd_ = [], [], []
    for i in range(1, len(cs)):
        trs.append(max(hs[i] - ls[i], abs(hs[i] - cs[i - 1]), abs(ls[i] - cs[i - 1])))
        up, dn = hs[i] - hs[i - 1], ls[i - 1] - ls[i]
        pd_.append(up if up > dn and up > 0 else 0.0)
        nd_.append(dn if dn > up and dn > 0 else 0.0)
    atr, p, q = sum(trs[:n]), sum(pd_[:n]), sum(nd_[:n])
    dx = []
    for i in range(n, len(trs)):
        atr = atr - atr / n + trs[i]
        p = p - p / n + pd_[i]
        q = q - q / n + nd_[i]
        pdi, ndi = 100 * p / atr, 100 * q / atr
        dx.append(100 * abs(pdi - ndi) / max(pdi + ndi, 1e-9))
    a = sum(dx[:n]) / n
    for x in dx[n:]:
        a = (a * (n - 1) + x) / n
    return a


def test_adx_matches_wilder_reference():
    from core.regime import _adx, mock_bars
    for sym, spot in (("SPX", 6000.), ("QQQ", 525.), ("RUT", 2300.)):
        b = mock_bars(sym, spot, TODAY, n=300)
        hs, ls, cs = [x[2] for x in b], [x[3] for x in b], [x[4] for x in b]
        assert abs(_adx(hs, ls, cs) - _ref_wilder_adx(hs, ls, cs)) < 1e-9


def test_adx_reads_strong_trend():
    from core.regime import _adx
    cs = [100 * math.exp(0.01 * i) for i in range(80)]
    hs = [c * 1.001 for c in cs]
    ls = [c * 0.999 for c in cs]
    assert _adx(hs, ls, cs) > 30


# --------------------------------------------------------------- surface ----
def test_iv_cm_variance_interpolation():
    from core.models import Slice
    from core.surface import iv_cm
    s7 = Slice(TODAY + timedelta(days=7), 7, 6000, 0.20)
    s21 = Slice(TODAY + timedelta(days=21), 21, 6000, 0.16)
    va, vb = .04 * 7, .0256 * 21
    exp9 = math.sqrt((va + (vb - va) * 2 / 14) / 9)     # hand-computed
    assert abs(iv_cm([s7, s21], 9) - exp9) < 1e-12
    assert iv_cm([s7, s21], 5) == 0.20                  # clamped ends
    assert iv_cm([s7, s21], 30) == 0.16


def test_forward_vol_hand_case():
    from core.surface import forward_vol
    exp = math.sqrt((0.0625 * 60 - 0.04 * 30) / 30)     # 20@30d, 25@60d
    assert abs(forward_vol(.20, 30 / 365, .25, 60 / 365) - exp) < 1e-12


# --------------------------------------------------------------- metrics ----
def test_breakeven_interpolated_single_call():
    from core.models import Leg
    from core.pricing import bs_price, struct_metrics
    K, iv, dte = 6000., .15, 30
    entry = bs_price(6000., K, dte / 365, iv, "C", q=Q)
    m = struct_metrics(6000., [Leg("C", K, TODAY + timedelta(days=dte), 1, iv)],
                       TODAY, q=Q)
    assert len(m["breakevens"]) == 1
    assert abs(m["breakevens"][0] - (K + entry)) < 0.05  # was ~7 pts off


def test_calendar_still_two_breakevens():
    from core.models import Leg
    from core.pricing import struct_metrics
    legs = [Leg("P", 6000, TODAY + timedelta(days=14), -1, .15),
            Leg("P", 6000, TODAY + timedelta(days=35), 1, .145)]
    m = struct_metrics(6000., legs, TODAY, q=Q)
    assert len(m["breakevens"]) == 2 and m["max_profit"] > 0 > m["max_loss"]


# ----------------------------------------------------------------- chain ----
def test_k25_hits_25_delta():
    from core.chain import k25
    from core.pricing import bs_greeks
    t, iv = 30 / 365, .14
    for cp in ("P", "C"):
        k = k25(6000., iv, t, cp, q=Q)
        d = abs(bs_greeks(6000., k, t, iv, cp, r=R, q=Q)["delta"])
        assert abs(d - 0.25) < 0.005                    # was .219P / .283C


# ---------------------------------------------------------------- ranker ----
def test_ranker_vrp_negative_keeps_caution_and_leads_debit():
    from core.models import Context
    from selection.ranker import family_priority
    reg = {"trend": "RNG", "vol_state": "NRM", "iv_pctl": 40., "iv30": 14.,
           "iv_chg_pct": 0., "vrp": -1.0, "rv7": 15., "rv21": 15.,
           "rv_falling": False, "gamma_score": 1, "gamma": "+g", "adx": 15.,
           "bias": 0, "ac20": 0., "term": {"verdict": "FLAT"}}
    ctx = Context("SPX", 6000., TODAY, [], [6000.], regime=reg,
                  events={"fomc_dte": 999, "fomc_in_front": False,
                          "opex_week": False, "post_opex": False,
                          "ex_div": False}, gates=[])
    fams, verdict = family_priority(ctx)
    assert verdict.startswith("CAUTION")                # not downgraded
    assert fams[0][0] == "calendar"                     # debit leads when VRP<=0


# --------------------------------------------------------------- sentinel ---
def test_sentinel_skew_label_matches_fvs_convention():
    import sentinel as S
    base = {"symbol": "SPX", "trend": "RNG", "vol_state": "NRM", "iv_pctl": 50.,
            "iv30": 14., "vrp": 1., "rv7": 13., "rv21": 13., "rv_falling": True,
            "gamma_score": 1, "gamma": "+g", "adx": 15., "bias": 0, "ac20": 0.}
    hp = S.RegimeView.from_fvs(base, {"verdict": "CONTANGO", "rr25_30d": +6.}).headline()
    hc = S.RegimeView.from_fvs(base, {"verdict": "CONTANGO", "rr25_30d": -6.}).headline()
    assert "put-skew" in hp and "call-skew" in hc


# ---------------------------------------------- frequency calibration (T1-T4)
def _freq_ctx(reg_over, pairs=None):
    from core.models import Context
    reg = {"trend": "RNG", "vol_state": "NRM", "iv_pctl": 40., "iv30": 14.,
           "iv_chg_pct": 0., "vrp": 1.5, "rv7": 12., "rv21": 12.5,
           "rv_falling": True, "gamma_score": 1, "gamma": "+g", "adx": 15.,
           "bias": 0, "ac20": 0., "term": {"verdict": "FLAT"}}
    reg.update(reg_over)
    ctx = Context("SPX", 6000., TODAY, [], [6000.], regime=reg,
                  events={"fomc_dte": 999, "fomc_in_front": False,
                          "opex_week": False, "post_opex": False,
                          "ex_div": False}, gates=[])
    if pairs is not None:
        ctx.pairs = pairs
    return ctx


def test_t1_nrm_trend_routes_to_diagonal():
    from selection.ranker import family_priority
    fams, verdict = family_priority(_freq_ctx({"trend": "UP", "adx": 26.}))
    assert fams[0][0] == "diagonal"          # the previously-missing cell
    assert verdict == "TRADE"


def test_t2_str_rich_and_calming_graduates_to_bwb():
    from selection.ranker import family_priority
    fams, _ = family_priority(_freq_ctx({"vol_state": "STR", "iv_pctl": 90.,
                                         "vrp": 3.0, "rv_falling": True}))
    assert any(k == "bwb" for k, _ in fams)


def test_t2_str_unpaid_does_not_graduate():
    from selection.ranker import family_priority
    fams, _ = family_priority(_freq_ctx({"vol_state": "STR", "iv_pctl": 90.,
                                         "vrp": 1.0, "rv_falling": True}))
    assert not any("Stressed-but-rich" in why for _, why in fams)


def test_t3_pair_table_flags_fomc_between_instead_of_killing():
    from core.models import Slice
    from core.surface import pair_table
    today = date(2026, 7, 2)                 # next FOMC: 2026-07-29
    f = Slice(date(2026, 7, 17), 15, 6000, 0.16)     # front before FOMC
    b = Slice(date(2026, 8, 7), 36, 6000, 0.155)     # back after FOMC
    rows = pair_table([f, b], today)
    assert len(rows) == 1                    # pre-T3 this pair was excluded
    assert rows[0]["fomc_between"] is True


def test_t4_zero_pair_days_get_single_expiry_fallback():
    from selection.ranker import family_priority
    ctx = _freq_ctx({"vrp": -1.0, "term": {"verdict": "CONTANGO"}}, pairs=[])
    fams, verdict = family_priority(ctx)
    assert fams[0][0] == "calendar"          # doctrine lead unchanged
    assert any(k == "butterfly" for k, _ in fams)    # but never an empty board
    assert verdict.startswith("CAUTION")
