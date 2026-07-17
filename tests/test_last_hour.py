import store.campaigns as campaign_module


def _card(data, key):
    return next(c for c in data["cards"] if c["strategy"] == key)


def test_last_hour_spx_is_focused_and_persists_candidates(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "last-hour.sqlite"))
    campaign_module._STORE = None
    import webapp

    client = webapp.app.test_client()
    response = client.get(
        "/api/last-hour/decision?symbol=SPX&mode=mock&account=MOCK-A"
        "&mandate=margin&nlv=100000&te_completed=3&tz_paper=3"
    )
    assert response.status_code == 200
    data = response.get_json()
    assert {c["strategy"] for c in data["cards"]} == {
        "fly_bull", "fly_chop", "fly_bear", "timeedge"
    }
    assert all(c["candidate_id"] for c in data["cards"])
    timeedge = _card(data, "timeedge")
    assert timeedge["legs_raw"][0]["expiry"] != timeedge["legs_raw"][1]["expiry"]
    assert data["progression"]["timezone_unlocked"] is True
    bull = _card(data, "fly_bull")
    assert 1.0 <= bull["greeks"]["delta"] <= 2.0
    assert any("Confirm a controlled red-day pullback" in reason
               for reason in bull["wait_reasons"])

    confirmed = client.get(
        "/api/last-hour/decision?symbol=SPX&mode=mock&account=MOCK-A"
        "&mandate=margin&nlv=100000&trigger=bull_pullback"
    ).get_json()
    assert not any("Confirm a controlled red-day pullback" in reason
                   for reason in _card(confirmed, "fly_bull")["wait_reasons"])


def test_last_hour_rut_halves_size_and_gates_timezone(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "last-hour-rut.sqlite"))
    campaign_module._STORE = None
    import webapp

    locked = webapp.app.test_client().get(
        "/api/last-hour/decision?symbol=RUT&mode=mock&account=MOCK-A"
        "&mandate=margin&nlv=100000&te_completed=2&tz_paper=3"
    ).get_json()
    timezone = _card(locked, "timezone")
    assert timezone["status"] == "WAIT"
    assert any("Progression lock" in reason for reason in timezone["wait_reasons"])
    assert all(c["governor"]["size"] == "HALF" for c in locked["cards"])


def test_cash_mandate_blocks_time_spreads_but_not_single_expiry_flies(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "last-hour-cash.sqlite"))
    campaign_module._STORE = None
    import webapp

    data = webapp.app.test_client().get(
        "/api/last-hour/decision?symbol=SPX&mode=mock&account=MOCK-B"
        "&mandate=cash&nlv=100000&te_completed=3&tz_paper=3"
    ).get_json()
    assert any("blocks multi-expiry" in reason
               for reason in _card(data, "timeedge")["wait_reasons"])
    for key in ("fly_bull", "fly_chop", "fly_bear"):
        assert not any("blocks multi-expiry" in reason
                       for reason in _card(data, key)["wait_reasons"])


def test_last_hour_page_is_default_and_links_back_to_research():
    import webapp

    client = webapp.app.test_client()
    page = client.get("/").get_data(as_text=True)
    assert "Last Hour Trade Desk" in page
    assert "15:00–15:40 ET" in page
    assert 'href="/research"' in page
    assert "TE Playbook" in client.get("/research").get_data(as_text=True)
