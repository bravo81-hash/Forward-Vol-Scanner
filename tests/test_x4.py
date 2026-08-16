from core.context import build_context
from selection.x4 import build_x4


def _ctx():
    return build_context("SPX", "mock")


def test_x4_builds_three_listed_single_expiry_structures():
    out = build_x4(_ctx())
    assert {c["strategy"] for c in out["cards"]} == {"v14", "v17", "v22"}
    assert 60 <= out["inputs"]["dte"] <= 85
    expiry = out["inputs"]["expiry"]
    assert all({leg["expiry"] for leg in c["legs_raw"]} == {expiry}
               for c in out["cards"])
    assert all(c["manual_test_allowed"] and not c["tws_stage_allowed"]
               for c in out["cards"])


def test_v14_v17_have_separate_tail_put_and_v22_solves_call_ratio():
    cards = {c["strategy"]: c for c in build_x4(_ctx())["cards"]}
    for key in ("v14", "v17"):
        legs = cards[key]["legs_raw"]
        assert len(legs) == 4 and all(leg["cp"] == "P" for leg in legs)
        assert sorted(leg["qty"] for leg in legs) == [-2, 1, 1, 1]
    v22 = cards["v22"]["legs_raw"]
    calls = [leg for leg in v22 if leg["cp"] == "C"]
    puts = sorted((leg for leg in v22 if leg["cp"] == "P"),
                  key=lambda leg: leg["strike"])
    assert len(calls) == 1 and calls[0]["qty"] == 1
    ratio = puts[0]["qty"]
    assert 1 <= ratio <= 12
    assert [leg["qty"] for leg in puts] == [ratio, -2 * ratio, ratio]
    assert abs((puts[1]["strike"] - puts[0]["strike"]) -
               (puts[2]["strike"] - puts[1]["strike"])) <= 5


def test_x4_overrides_drive_the_recommendation_deterministically():
    ctx = _ctx()
    assert build_x4(ctx, setup="whippy", iv_state="spike",
                    posture="allweather")["recommended_strategy"] == "v14"
    assert build_x4(ctx, setup="support", iv_state="normal",
                    posture="bullish")["recommended_strategy"] == "v17"
    assert build_x4(ctx, setup="range", iv_state="elevated",
                    posture="vol")["recommended_strategy"] == "v22"


def test_x4_waits_when_front_term_is_inverted():
    ctx = _ctx()
    ctx.regime["term"]["verdict"] = "INVERTED FRONT"
    out = build_x4(ctx)
    assert out["action"].startswith("WAIT")
    assert "inverted" in out["action"]


def test_x4_page_and_mock_api_are_available():
    import webapp
    from web import shared

    client = webapp.app.test_client()
    assert client.get("/x4").status_code == 200
    response = client.get(
        "/api/x4/build?symbol=SPX&mode=mock&setup=range&iv_state=elevated&posture=vol")
    assert response.status_code == 200
    data = response.get_json()
    assert data["recommended_strategy"] == "v22"
    assert all(card["optionstrat_url"].startswith("https://optionstrat.com/")
               for card in data["cards"])


def test_x4_live_api_reprices_exact_candidates(monkeypatch):
    import webapp
    from web import shared

    ctx = _ctx()
    ctx.mode = "live"
    ctx.data.update(session=ctx.today.isoformat(), fresh=True,
                    as_of_time="15:30:00", captured_at="live-test")
    profile = {"account": "DU123", "nlv": 100_000, "cash_account": False,
               "block_multi_expiry": False}
    monkeypatch.setattr(shared, "v3_context",
                        lambda *args, **kwargs: (ctx, profile, []))
    monkeypatch.setattr(shared, "with_ib", lambda fn: fn(object()))

    def fake_reprice(_ib, _symbol, _spot, _today, cards, **_kwargs):
        for card in cards:
            card["mid_src"] = "live"
            card["net_mid"] = -1.25

    monkeypatch.setattr(shared, "reprice_cards", fake_reprice)
    data = webapp.app.test_client().get(
        "/api/x4/build?symbol=SPX&mode=live&account=DU123").get_json()
    assert data["live_capture"]["status"] == "TWS_CONNECTED"
    assert all(card["mid_src"] == "live" for card in data["cards"])
    assert next(c for c in data["cards"] if c["strategy"] == "v17")[
        "upside_plateau"] == 125
