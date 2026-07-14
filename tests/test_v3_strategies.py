from core.context import build_context
from config.loader import account_profile
from selection.lab import strategy_lab
from selection.smsf import smsf_shortlist
from strategies import REGISTRY


def _ctx(account="MOCK-B", nlv=100_000):
    ctx = build_context("SPX", "mock")
    ctx.mandate = account_profile(account, nlv)
    ctx.book = {"nlv": nlv, "greeks": {"delta": 0, "gamma": 0, "theta": 0, "vega": 0}}
    return ctx


def test_new_strategy_factories_registered_and_construct():
    keys = {"balanced_fly", "iron_fly", "otm_put_fly", "call_bwb",
            "m3_bwb_call", "target_fly"}
    assert keys <= set(REGISTRY)
    ctx = _ctx()
    for key in keys - {"target_fly"}:
        cards = REGISTRY[key].propose(ctx)
        assert cards, key
        assert all(c.legs and c.cash_required is not None for c in cards)
    assert REGISTRY["target_fly"].propose_for_bias(ctx, 1)


def test_m3_is_same_expiry_and_has_itm_call():
    card = REGISTRY["m3_bwb_call"].propose(_ctx())[0]
    assert len(card.legs) == 4
    assert len({leg.expiry for leg in card.legs}) == 1
    calls = [leg for leg in card.legs if leg.cp == "C"]
    assert len(calls) == 1 and calls[0].qty > 0


def test_call_bwb_is_all_calls_and_broken_upper_wing():
    card = REGISTRY["call_bwb"].propose(_ctx())[0]
    assert all(leg.cp == "C" for leg in card.legs)
    legs = sorted(card.legs, key=lambda leg: leg.strike)
    assert legs[1].qty == -2
    assert legs[2].strike - legs[1].strike > legs[1].strike - legs[0].strike


def test_iron_fly_has_short_atm_put_and_call():
    card = REGISTRY["iron_fly"].propose(_ctx())[0]
    shorts = [leg for leg in card.legs if leg.qty < 0]
    assert {leg.cp for leg in shorts} == {"C", "P"}
    assert len({leg.strike for leg in shorts}) == 1


def test_gate_s_intents_surface_chat_strategies():
    assert "m3_bwb_call" in {c["strategy"] for c in smsf_shortlist(_ctx(), "bull", "MOCK-B", 100_000)["cards"]}
    assert "iron_fly" in {c["strategy"] for c in smsf_shortlist(_ctx(), "neutral", "MOCK-B", 100_000)["cards"]}
    bear = {c["strategy"] for c in smsf_shortlist(_ctx(), "bear", "MOCK-B", 100_000)["cards"]}
    assert {"otm_put_fly", "target_fly"} <= bear


def test_strategy_lab_cash_and_margin_mandates():
    cash = strategy_lab(_ctx("MOCK-B"), "neutral", "MOCK-B", 100_000)
    margin = strategy_lab(_ctx("MOCK-A", 250_000), "neutral", "MOCK-A", 250_000)
    cash_keys = {c["strategy"] for c in cash["cards"]}
    margin_keys = {c["strategy"] for c in margin["cards"]}
    assert not cash_keys & {"calendar", "double_calendar", "diagonal"}
    assert {"calendar", "double_calendar", "diagonal"} <= margin_keys
    assert {"balanced_fly", "iron_fly", "otm_put_fly", "call_bwb",
            "m3_bwb_call", "target_fly"} <= cash_keys


def test_hypothesis_cards_are_manual_testable_not_live_approved():
    out = smsf_shortlist(_ctx(), "bull", "MOCK-B", 100_000)
    assert all(c["manual_test_allowed"] for c in out["cards"])
    assert all(c["evidence"]["status"] == "HYPOTHESIS" for c in out["cards"])
