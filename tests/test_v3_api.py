import store.campaigns as campaign_module


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
            "m3_bwb_call", "target_fly"} <= keys
    assert not keys & {"calendar", "double_calendar", "diagonal"}
