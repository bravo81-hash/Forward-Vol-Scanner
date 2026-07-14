from config.loader import account_profile, hypothesis, hypothesis_config, risk_config


def test_hypothesis_registry_valid_and_unique():
    cfg = hypothesis_config()
    ids = [h["id"] for h in cfg["hypotheses"]]
    assert len(ids) == len(set(ids)) >= 13
    assert set(h["status"] for h in cfg["hypotheses"]) <= set(cfg["statuses"])


def test_known_hypothesis_lookup():
    assert hypothesis("H007")["name"] == "m3_bwb_call_cash_account_substitute"


def test_account_profiles_centralise_cash_mandate():
    cash = account_profile("MOCK-B")
    margin = account_profile("MOCK-A")
    assert cash["cash_account"] and cash["block_multi_expiry"]
    assert not margin["cash_account"] and not margin["block_multi_expiry"]


def test_risk_config_has_every_governor_limit():
    cfg = risk_config()
    assert {"delta", "gamma", "vega"} <= set(cfg["per_100k"])
    assert cfg["limits"]["max_campaign_risk_pct_nlv"] > 0
