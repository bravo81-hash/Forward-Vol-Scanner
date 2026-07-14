from datetime import date

from config.loader import account_profile
from core.context import build_context
from selection.campaign_ranker import rank_campaign


def _ctx(*, account="MOCK-B", intent="neutral", iv_band="NRM", term="FLAT",
         rr=0, vrp=4, trend="RNG", spot=5600, iv30=20):
    bias = {"bull": 1, "neutral": 0, "bear": -1}[intent]
    ctx = build_context("SPX", "mock", today=date(2025, 3, 17), manual={
        "historical": True, "as_of_time": "15:30", "spot": spot,
        "iv30": iv30, "rv21": max(5, iv30 - vrp), "vrp_fwd": vrp,
        "rr25_30d": rr, "iv_band": iv_band, "term": term,
        "trend": trend, "event": "NONE", "bias": bias,
    })
    ctx.mandate = account_profile(account)
    ctx.book = {"nlv": ctx.mandate["nlv"],
                "greeks": {"delta": 0, "gamma": 0, "theta": 0, "vega": 0}}
    return ctx


def _top(**kwargs):
    intent = kwargs.pop("intent", "neutral")
    ctx = _ctx(intent=intent, **kwargs)
    return rank_campaign(ctx, intent, ctx.mandate["account"], ctx.mandate["nlv"], True)


def test_conditional_leaders_change_with_historical_state():
    assert _top(rr=7, vrp=5)["cards"][0]["strategy"] == "bwb"
    assert _top(iv_band="ELV", rr=8, vrp=6, intent="bear")["cards"][0]["strategy"] == "otm_put_fly"
    assert _top(rr=7, vrp=5, intent="bull")["cards"][0]["strategy"] == "m3_bwb_call"
    assert _top(rr=-1, vrp=5)["cards"][0]["strategy"] == "call_bwb"


def test_cash_and_margin_rank_the_high_iv_flat_skew_trade_differently():
    cash = _top(iv_band="ELV", rr=0, vrp=5, account="MOCK-B")
    margin = _top(iv_band="ELV", rr=0, vrp=5, account="MOCK-A")
    cash_scores = {c["strategy"]: c["family_edge_score"] for c in cash["cards"]}
    margin_scores = {c["strategy"]: c["family_edge_score"] for c in margin["cards"]}
    assert cash_scores["iron_fly"] < cash_scores["balanced_fly"]
    assert margin_scores["iron_fly"] >= 85


def test_defensive_state_blocks_all_carry_families():
    out = _top(term="INVERTED FRONT", iv_band="STR", vrp=-3, intent="bear")
    assert out["market_state"]["name"] == "CRISIS"
    assert {c["strategy"] for c in out["cards"]} <= {"debit_spread", "target_fly"}
    blocked = {x["strategy"] for x in out["excluded"]}
    assert {"bwb", "condor", "calendar", "iron_fly"} <= blocked


def test_shortlist_and_full_lab_share_one_ranking_policy():
    ctx = _ctx(account="MOCK-A", intent="bull", iv_band="CMP", vrp=0, trend="UP")
    short = rank_campaign(ctx, "bull", "MOCK-A", 250_000, False)
    full = rank_campaign(ctx, "bull", "MOCK-A", 250_000, True)
    assert short["policy_id"] == full["policy_id"] == "campaign-ranker-v4"
    assert [c["strategy"] for c in short["cards"]] == list(dict.fromkeys(
        c["strategy"] for c in full["cards"]))[:6]
