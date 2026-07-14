import store.campaigns as campaign_module


def test_historical_snapshot_endpoint(monkeypatch):
    import core.historical
    import webapp

    expected = {"symbol": "SPX", "entry_date": "2025-03-17", "spot": 5600,
                "intent": "neutral", "confidence": "HIGH"}
    monkeypatch.setattr(core.historical, "auto_historical_snapshot",
                        lambda symbol, as_of: {**expected, "symbol": symbol,
                                               "entry_date": as_of.isoformat()})
    client = webapp.app.test_client()
    response = client.get("/api/v3/historical-snapshot?symbol=SPX&entry_date=2025-03-17")
    assert response.status_code == 200
    assert response.get_json() == expected
    assert client.get(
        "/api/v3/historical-snapshot?symbol=SPX&entry_date=2025-03-16"
    ).status_code == 400


def test_v3_mock_end_to_end(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "v3.sqlite"))
    campaign_module._STORE = None
    import webapp
    client = webapp.app.test_client()
    res = client.get("/api/v3/opportunities?symbol=SPX&intent=bull&mode=mock&account=MOCK-B&nlv=100000")
    assert res.status_code == 200
    data = res.get_json()
    assert data["cards"] and all(c.get("candidate_id") for c in data["cards"])
    assert "m3_bwb_call" in {c["strategy"] for c in data["cards"]}

    cid = data["cards"][0]["candidate_id"]
    staged = client.post("/api/v3/stage", json={"candidate_id": cid, "quantity": 1,
                                                 "legs": [{"tampered": True}]}).get_json()
    assert staged["status"] == "MockStaged"
    assert staged["legs"] == data["cards"][0]["legs_raw"]

    created = client.post("/api/v3/campaigns", json={"candidate_id": cid,
                                                      "quantity": 1,
                                                      "test_mode": "optionnet"})
    assert created.status_code == 201
    campaign_id = created.get_json()["id"]
    saved = client.post(f"/api/v3/campaigns/{campaign_id}/manual-test",
                        json={"source": "OptionNet Explorer", "setup_rating": 5,
                              "result_pct": 15, "notes": "test"})
    assert saved.status_code == 200
    assert client.get("/api/v3/evidence").status_code == 200
    defaults = client.get("/api/v3/defaults").get_json()
    assert defaults["timezone"] == "America/New_York"
    assert client.get("/campaigns").status_code == 200


def test_v3_lab_exposes_all_cash_permitted_families(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "lab.sqlite"))
    campaign_module._STORE = None
    import webapp
    d = webapp.app.test_client().get(
        "/api/v3/opportunities?symbol=SPX&intent=neutral&mode=mock&account=MOCK-B&nlv=100000&lab=true"
    ).get_json()
    keys = {c["strategy"] for c in d["cards"]}
    assert {"balanced_fly", "iron_fly", "otm_put_fly", "call_bwb",
            "m3_bwb_call"} <= keys
    assert "target_fly" not in keys
    assert any(x["strategy"] == "target_fly" for x in d["excluded"])
    assert not keys & {"calendar", "double_calendar", "diagonal"}


def test_historical_one_context_is_date_aware_and_ranked(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "historical.sqlite"))
    campaign_module._STORE = None
    import webapp
    q = ("/api/v3/opportunities?symbol=SPX&intent=bear&mode=mock&account=MOCK-B"
         "&nlv=100000&entry_date=2025-03-17&entry_time=15:30&spot=5600"
         "&iv30=28&rv21=20&vrp_fwd=8&rr25=7&iv_band=ELV"
         "&term=CONTANGO&trend=DN&event=NONE&lab=true")
    res = webapp.app.test_client().get(q)
    assert res.status_code == 200
    data = res.get_json()
    assert data["data"]["session"] == "2025-03-17"
    assert data["data"]["as_of_time"] == "15:30"
    assert data["spot"] == 5600
    assert data["test_session_id"].startswith("ONE-")
    assert [c["rank_score"] for c in data["cards"]] == sorted(
        [c["rank_score"] for c in data["cards"]], reverse=True)
    assert all(min(leg["expiry"] for leg in c["legs_raw"]) > "2025-03-17"
               for c in data["cards"])


def test_inverted_front_enforces_zero_new_carry(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "inverted.sqlite"))
    campaign_module._STORE = None
    import webapp
    base = ("/api/v3/opportunities?symbol=SPX&mode=mock&account=MOCK-B&nlv=100000"
            "&entry_date=2025-03-17&entry_time=15:30&spot=5600&iv30=35&rv21=38"
            "&vrp_fwd=-3&rr25=8&iv_band=STR&term=INVERTED%20FRONT&trend=DN&event=NONE")
    neutral = webapp.app.test_client().get(base + "&intent=neutral").get_json()
    assert not neutral["cards"] and "STAND ASIDE" in neutral["action"]
    bull = webapp.app.test_client().get(base + "&intent=bull&lab=true").get_json()
    assert {c["strategy"] for c in bull["cards"]} <= {"debit_spread", "target_fly"}


def test_two_strategies_form_one_complete_matched_date_session(monkeypatch, tmp_path):
    monkeypatch.setenv("FVS_CAMPAIGN_DB", str(tmp_path / "matched-api.sqlite"))
    campaign_module._STORE = None
    import webapp
    client = webapp.app.test_client()
    q = ("/api/v3/opportunities?symbol=SPX&intent=neutral&mode=mock&account=MOCK-B"
         "&nlv=100000&entry_date=2025-03-17&entry_time=15:30&spot=5600"
         "&iv30=20&rv21=16&vrp_fwd=4&rr25=0&iv_band=NRM"
         "&term=FLAT&trend=RNG&event=NONE")
    data = client.get(q).get_json()
    campaigns = []
    for card in data["cards"][:2]:
        campaign = client.post("/api/v3/campaigns", json={
            "candidate_id": card["candidate_id"], "quantity": 1,
            "test_mode": "optionnet_matched_date"}).get_json()
        campaigns.append(campaign)
        saved = client.post(f"/api/v3/campaigns/{campaign['id']}/manual-test", json={
            "source": "OptionNet Explorer", "result_pct": 5,
            "parameters": {"actual_legs": "ONE legs", "matched_date": True}})
        assert saved.status_code == 200
    assert {c["card"]["test_session_id"] for c in campaigns} == {data["test_session_id"]}
    evidence = client.get("/api/v3/evidence").get_json()
    assert evidence["complete_matched_sessions"] == 1
