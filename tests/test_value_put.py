from __future__ import annotations


def test_mock_value_scan_has_qualified_and_rejected_rows():
    from value_put.service import scan_value_puts

    result = scan_value_puts(source="mock", symbols=["BAC", "AAL"])
    rows = {row["symbol"]: row for row in result["rows"]}
    assert result["policy_id"] == "value-entry-put-v1"
    assert rows["BAC"]["candidate"]["status"] == "QUALIFIED"
    assert rows["AAL"]["candidate"]["status"] == "REJECTED"
    assert any("quality" in reason for reason in rows["AAL"]["candidate"]["blocks"])


def test_cash_secured_metrics_do_not_treat_buying_power_as_risk():
    from value_put.service import scan_value_puts

    row = scan_value_puts(source="mock", symbols=["BAC"])["rows"][0]
    candidate = row["candidate"]
    assert candidate["assignment_capital"] == candidate["strike"] * 100
    assert candidate["sizing"]["per_contract_capital"] == candidate["assignment_capital"]
    assert candidate["return_on_buying_power_pct"] > candidate["cash_secured_return_pct"]
    assert candidate["sizing"]["max_contracts"] == 1


def test_reviewed_acquisition_price_override_is_authoritative():
    from value_put.service import scan_value_puts

    result = scan_value_puts(
        source="mock", symbols=["BAC"], overrides={"BAC": 35.0})
    row = result["rows"][0]
    assert row["valuation"]["acquisition_price"] == 35.0
    assert row["valuation"]["acquisition_source"] == "user override"
    assert row["candidate"]["net_basis"] <= 35.0


def test_defined_risk_mode_uses_max_loss_and_protective_put():
    from value_put.service import scan_value_puts

    result = scan_value_puts(
        source="mock", symbols=["BAC"], mode="defined_risk")
    candidate = result["rows"][0]["candidate"]
    assert candidate["long_strike"] < candidate["strike"]
    assert candidate["return_basis"] == "defined max loss"
    assert candidate["max_loss"] < candidate["assignment_capital"]
    assert candidate["sizing"]["per_contract_capital"] == candidate["max_loss"]


def test_value_scan_rejects_invalid_mode_and_dte():
    import pytest

    from value_put.service import scan_value_puts

    with pytest.raises(ValueError):
        scan_value_puts(source="mock", mode="naked")
    with pytest.raises(ValueError):
        scan_value_puts(source="mock", min_dte=400, max_dte=100)


def test_value_put_web_page_and_api():
    import webapp

    client = webapp.app.test_client()
    page = client.get("/value-puts")
    assert page.status_code == 200
    assert b"Value Entry Put Scanner" in page.data
    trailing_slash_page = client.get("/value-puts/")
    assert trailing_slash_page.status_code == 200
    response = client.post("/api/value-puts/scan", json={
        "source": "mock", "symbols": ["BAC", "AAL"],
        "mode": "cash_secured", "nlv": 100_000, "available_cash": 50_000,
    })
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["summary"]["symbols_scanned"] == 2
    assert payload["score_weights"]["valuation_margin"] == 25


def test_automated_universe_discovery_ranks_and_selects_without_tickers():
    from value_put.discovery import discover_value_universe

    result = discover_value_universe(source="mock", limit=12)
    rows = {row["symbol"]: row for row in result["rows"]}
    assert result["policy_id"] == "value-universe-discovery-v1"
    assert result["universe_size"] > 25
    assert 1 <= len(result["selected_symbols"]) <= 12
    assert "BAC" in rows and rows["BAC"]["model"] == "financial company"
    assert rows["AAL"]["status"] == "EXCLUDED"
    assert all(
        rows[symbol]["status"] in {"ELIGIBLE", "REVIEW"}
        for symbol in result["selected_symbols"]
    )


def test_discovery_api_and_page_are_available():
    import webapp

    client = webapp.app.test_client()
    page = client.get("/value-puts/")
    assert b"Universe Discovery" in page.data
    response = client.post("/api/value-puts/discover", json={
        "source": "mock",
        "limit": 10,
        "min_market_cap": 5_000_000_000,
        "min_average_dollar_volume": 50_000_000,
        "min_quality": 68,
        "max_leverage": 3,
        "max_price_to_fcf": 35,
    })
    payload = response.get_json()
    assert response.status_code == 200
    assert payload["summary"]["selected"] <= 10
    assert payload["selected_symbols"]


def test_discovery_api_validates_policy_inputs():
    import webapp

    client = webapp.app.test_client()
    response = client.post("/api/value-puts/discover", json={
        "source": "mock", "limit": 26,
    })
    assert response.status_code == 400
    assert "limit" in response.get_json()["error"]


def test_value_put_api_validates_inputs():
    import webapp

    client = webapp.app.test_client()
    response = client.post("/api/value-puts/scan", json={
        "source": "mock", "mode": "wrong",
    })
    assert response.status_code == 400
    assert "mode" in response.get_json()["error"]


def test_tws_finalist_validation_is_quote_and_what_if_only(monkeypatch):
    import value_put.tws as tws_module

    class Margin:
        initMarginChange = "850.25"
        maintMarginChange = "725.50"
        equityWithLoanChange = "-3850"
        warningText = ""

    class FakeIB:
        def qualifyContracts(self, *contracts):
            for index, contract in enumerate(contracts, 1):
                contract.conId = index

        def whatIfOrder(self, contract, order):
            assert order.transmit is False
            return Margin()

    def fake_quotes(_ib, contracts, **_kwargs):
        return {contract.conId: {
            "bid": 3.70, "ask": 4.10, "iv": .44,
            "greeks": {"delta": -.19}, "oi": 850, "volume": 22,
        } for contract in contracts}

    monkeypatch.setattr(tws_module, "quote_many", fake_quotes)
    result = tws_module.validate_candidate_tws(FakeIB(), "BAC", {
        "expiry": "2027-06-18", "strike": 42.5,
    })
    assert result["net_credit"] == 3.8
    assert result["delta"] == .19
    assert result["what_if"]["initial_margin_change"] == 850.25
    assert result["transmitted"] is False and result["staged"] is False
