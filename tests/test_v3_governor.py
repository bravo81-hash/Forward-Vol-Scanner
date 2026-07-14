from portfolio.governor import aggregate_books, evaluate_candidate


def _card(**greeks):
    return {"strategy": "balanced_fly", "max_loss": -1.0,
            "cash_required": 100.0,
            "greeks": {"delta": greeks.get("delta", 0),
                       "gamma": greeks.get("gamma", 0),
                       "theta": greeks.get("theta", 1),
                       "vega": greeks.get("vega", 0)}}


def test_governor_uses_signed_post_trade_exposure():
    book = {"nlv": 100_000, "greeks": {"delta": 4, "gamma": 0, "theta": 0, "vega": 0}}
    result = evaluate_candidate(_card(delta=-2), book, 100_000, 6000)
    assert result["approved_lots"] >= 2
    assert abs(result["after"]["delta"]) <= result["limits"]["delta"]


def test_governor_binds_structural_risk():
    card = _card()
    card["max_loss"] = -30.0       # $3,000 > 2% of $100k
    result = evaluate_candidate(card, {}, 100_000, 6000)
    assert result["approved_lots"] == 0
    assert "structural_risk" in result["binding"]


def test_target_fly_uses_quarter_percent_risk_cap():
    card = _card()
    card.update(strategy="target_fly", max_loss=-3.0, cash_required=300)
    result = evaluate_candidate(card, {}, 100_000, 6000)
    assert result["approved_lots"] == 0


def test_aggregate_books_keeps_symbol_drilldown():
    out = aggregate_books([
        {"symbol": "SPX", "nlv": 200_000, "greeks": {"delta": 2, "gamma": 1, "theta": 5, "vega": 10}},
        {"symbol": "RUT", "nlv": 200_000, "greeks": {"delta": -1, "gamma": 0, "theta": 2, "vega": -5}},
    ])
    assert out["greeks"]["delta"] == 1
    assert set(out["by_symbol"]) == {"SPX", "RUT"}
    assert out["spx_equiv_delta"] != out["greeks"]["delta"]
