"""SMSF module — Gate S single-expiry selector and /api/smsf route."""
from __future__ import annotations

from datetime import date, timedelta

from core.models import Context, Slice
from selection.smsf import BUILD_SPECS, gate_s, smsf_verdict

TODAY = date(2026, 7, 7)


def mk_ctx(*, vrp_fwd=0.0, vrp=0.0, iv_pctl=50.0, vol_state="NRM",
           skew_rich=False, rr25=3.0, term_verdict="CONTANGO", bias=0,
           iv30=16.0, har_rv=16.0) -> Context:
    reg = {"iv30": iv30, "iv_pctl": iv_pctl, "vol_state": vol_state,
           "vrp": vrp, "vrp_fwd": vrp_fwd, "har_rv": har_rv, "rv21": 14.0,
           "bias": bias, "trend": "RNG",
           "term": {"verdict": term_verdict, "skew_rich": skew_rich,
                    "rr25_30d": rr25}}
    sl = [Slice(expiry=TODAY + timedelta(days=d), dte=d, atm_strike=100.0,
                atm_iv=iv30 / 100) for d in (10, 24, 38)]
    return Context(symbol="SPX", spot=100.0, today=TODAY, slices=sl,
                   strikes=[95.0, 100.0, 105.0], regime=reg,
                   events={}, gates=[], mode="mock", data={"note": "test"})


def top(ctx, bias):
    return gate_s(ctx, bias)[0][0][0]


# ------------------------------------------------------- decision table ----
def test_mid_iv_steep_skew_neutral_put_bwb():
    assert top(mk_ctx(skew_rich=True, rr25=6.0), 0) == "put_bwb"


def test_mid_iv_steep_skew_bull_m3():
    assert top(mk_ctx(skew_rich=True, rr25=6.0), 1) == "m3_bwb_call"


def test_mid_iv_steep_skew_bear_otm_put_fly():
    assert top(mk_ctx(skew_rich=True, rr25=6.0), -1) == "otm_put_fly"


def test_mid_iv_flat_skew_neutral_balanced_fly():
    assert top(mk_ctx(skew_rich=False, rr25=2.0), 0) == "balanced_fly"


def test_call_skew_bid_call_bwb():
    assert top(mk_ctx(skew_rich=False, rr25=-1.5), 0) == "call_bwb"
    assert top(mk_ctx(skew_rich=False, rr25=-1.5), 1) == "call_bwb"


def test_high_iv_steep_skew_otm_put_fly():
    order, notes = gate_s(mk_ctx(vol_state="ELV", skew_rich=True, rr25=7.0), 0)
    assert order[0][0] == "otm_put_fly"
    assert any("size down" in n.lower() for n in notes)


def test_high_iv_flat_skew_iron_fly():
    assert top(mk_ctx(vol_state="STR", skew_rich=False, rr25=2.0), 0) == "iron_fly"


def test_low_iv_neutral_stand_aside():
    ctx = mk_ctx(vol_state="CMP")
    order, notes = gate_s(ctx, 0)
    assert any("stand" in n.lower() for n in notes)
    out = smsf_verdict(ctx, "neutral")
    assert "STAND ASIDE" in out["action"]


def test_low_iv_with_bias_target_fly():
    assert top(mk_ctx(vol_state="CMP"), 1) == "target_fly"
    assert top(mk_ctx(vol_state="CMP"), -1) == "target_fly"


def test_backwardation_degross_overrides_all_rows():
    for vs, sk in (("NRM", True), ("ELV", False), ("CMP", False)):
        ctx = mk_ctx(vol_state=vs, skew_rich=sk, term_verdict="INVERTED FRONT")
        order, notes = gate_s(ctx, 0)
        assert order[0][0] == "target_fly"
        assert any("INVERTED" in n or "stress" in n.lower() for n in notes)
    out = smsf_verdict(mk_ctx(term_verdict="INVERTED FRONT"), "neutral")
    assert "ZERO NEW CARRY" in out["action"]


# ------------------------------------------------------------- verdict -----
def test_intent_override_beats_regime_bias():
    ctx = mk_ctx(skew_rich=True, rr25=6.0, bias=-2)
    out = smsf_verdict(ctx, "bull")
    assert out["bias"] == 1
    assert out["structures"][0]["key"] == "m3_bwb_call"


def test_auto_intent_uses_regime_bias():
    out = smsf_verdict(mk_ctx(skew_rich=True, rr25=6.0, bias=1), "auto")
    assert out["bias"] == 1 and out["structures"][0]["key"] == "m3_bwb_call"
    out = smsf_verdict(mk_ctx(skew_rich=True, rr25=6.0, bias=0), "auto")
    assert out["structures"][0]["key"] == "put_bwb"


def test_negative_forward_vrp_flags_unpaid_carry():
    out = smsf_verdict(mk_ctx(vrp_fwd=-1.5, skew_rich=True, rr25=6.0), "neutral")
    assert any("NEGATIVE" in n for n in out["notes"])


def test_vrp_flip_caution_note():
    ctx = mk_ctx(skew_rich=True, rr25=6.0)
    ctx.regime["vrp_flip"] = True
    out = smsf_verdict(ctx, "neutral")
    assert any("CAUTION" in n for n in out["notes"])


def test_cash_doctrine_note_always_present():
    out = smsf_verdict(mk_ctx(), "neutral")
    assert any("debit-side" in n for n in out["notes"])


def test_hard_gates_surfaced_not_suppressed():
    ctx = mk_ctx(skew_rich=True, rr25=6.0)
    ctx.gates.append({"hard": True, "code": "H1", "msg": "session stale"})
    out = smsf_verdict(ctx, "neutral")
    assert out["structures"]                       # advisory, never a block
    assert any("HARD GATE H1" in n for n in out["notes"])


def test_every_variant_has_full_build_spec():
    keys = {"label", "role", "dte", "body", "wings", "cash", "manage", "when"}
    for k, spec in BUILD_SPECS.items():
        assert keys <= set(spec), f"{k} missing {keys - set(spec)}"


def test_ranked_payload_carries_build_specs():
    out = smsf_verdict(mk_ctx(skew_rich=True, rr25=6.0), "neutral")
    s0 = out["structures"][0]
    assert s0["rank"] == 1 and s0["build"]["dte"] and s0["build"]["cash"]


# ---------------------------------------------------------------- route ----
def test_api_smsf_mock_route():
    import webapp
    c = webapp.app.test_client()
    r = c.get("/api/smsf?symbol=SPX&intent=neutral&mode=mock")
    assert r.status_code == 200
    d = r.get_json()
    assert d["action"].startswith("ACTION:")
    assert d["structures"] and "build" in d["structures"][0]


def test_api_smsf_bad_intent_rejected():
    import webapp
    c = webapp.app.test_client()
    assert c.get("/api/smsf?intent=sideways&mode=mock").status_code == 400
