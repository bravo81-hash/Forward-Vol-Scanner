from datetime import date

import pytest

import store.campaigns as campaign_module
from core.pricing import bs_price, q_for


def _decisions(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "risk-profiles.sqlite"))
    campaign_module._STORE = None
    import webapp

    client = webapp.app.test_client()
    query = "mode=mock&account=MOCK-A&mandate=margin&nlv=100000&te_completed=3&tz_paper=3"
    spx = client.get(f"/api/last-hour/decision?symbol=SPX&{query}").get_json()
    rut = client.get(f"/api/last-hour/decision?symbol=RUT&{query}").get_json()
    return client, spx, rut


def _value_at(profile, spot):
    index = min(range(len(profile["x"])), key=lambda i: abs(profile["x"][i] - spot))
    assert profile["x"][index] == pytest.approx(spot)
    return profile["front_expiry"][index]


def test_all_five_cards_have_exact_optionstrat_links_and_consistent_metrics(
        monkeypatch, tmp_path):
    client, spx, rut = _decisions(monkeypatch, tmp_path)
    cards = {card["strategy"]: card for data in (spx, rut) for card in data["cards"]}
    assert set(cards) == {"fly_bull", "fly_chop", "fly_bear", "timeedge", "timezone"}

    for key, card in cards.items():
        profile = card["risk_profile"]
        assert len(profile["x"]) > 200
        assert len(profile["x"]) == len(profile["front_expiry"]) == len(profile["t5"])
        assert profile["max_profit"] == card["max_profit"]
        assert profile["max_loss"] == card["max_loss"]
        assert profile["breakevens"] == card["breakevens"]
        assert card["optionstrat_url"].startswith("https://optionstrat.com/build/custom/")
        assert card["optionstrat_url"].count(",") == len(card["legs_raw"]) - 1
        assert " " not in card["optionstrat_url"]

    assert ".SPXW" in cards["timeedge"]["optionstrat_url"]
    assert ".RUTW" in cards["timezone"]["optionstrat_url"]
    assert "x2" in cards["fly_chop"]["optionstrat_url"]
    assert cards["timeedge"]["risk_profile"]["multi_expiry"] is True
    assert cards["timezone"]["risk_profile"]["multi_expiry"] is True
    assert all(cards[key]["risk_profile"]["multi_expiry"] is False
               for key in ("fly_bull", "fly_chop", "fly_bear"))
    assert "OptionStrat ↗" in client.get("/").get_data(as_text=True)


def test_single_expiry_fly_profiles_match_independent_intrinsic_payoff(
        monkeypatch, tmp_path):
    _, spx, _ = _decisions(monkeypatch, tmp_path)
    for card in spx["cards"]:
        if card["strategy"] not in ("fly_bull", "fly_chop", "fly_bear"):
            continue
        profile = card["risk_profile"]
        for strike in {float(leg["strike"]) for leg in card["legs_raw"]}:
            intrinsic = sum(
                int(leg["qty"]) * max(float(leg["strike"]) - strike, 0.0)
                for leg in card["legs_raw"]
            )
            assert _value_at(profile, strike) == pytest.approx(
                intrinsic - float(card["net_mid"]), abs=0.011)


def test_time_spread_front_expiry_profiles_retain_back_option_value(
        monkeypatch, tmp_path):
    _, spx, rut = _decisions(monkeypatch, tmp_path)
    today = date.fromisoformat(spx["data"]["session"])
    for data, key in ((spx, "timeedge"), (rut, "timezone")):
        card = next(card for card in data["cards"] if card["strategy"] == key)
        profile = card["risk_profile"]
        front = profile["front_dte"]
        calendar_strike = float(card["legs_raw"][-1]["strike"])
        independent = 0.0
        for leg in card["legs_raw"]:
            expiry = date.fromisoformat(leg["expiry"])
            remaining = max((expiry - today).days - front, 0)
            strike = float(leg["strike"])
            if remaining:
                value = bs_price(calendar_strike, strike, remaining / 365,
                                 float(leg["iv"]), leg["cp"], q=q_for(data["symbol"]))
            else:
                value = max(strike - calendar_strike, 0.0)
            independent += int(leg["qty"]) * value
        expected_pnl = independent - float(card["net_mid"])
        assert _value_at(profile, calendar_strike) == pytest.approx(expected_pnl, abs=0.011)
