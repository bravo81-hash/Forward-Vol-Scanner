"""Regression cover for the July 2026 delta-band recalibration.

The old band was 0.30 per-unit per $100k, i.e. 30 Risk-Navigator deltas per
$100k, which is roughly 200% of NLV in delta dollars at SPX ~6800. A 14-delta
book on $215k therefore read as "neutral". The band is now 5 underlying
deltas per $100k. These tests pin the specific book that exposed the bug, so
a future edit to the budget cannot silently reintroduce it.
"""
from portfolio.risk import BUDGET_PER_100K, budget_for


def test_delta_band_is_in_risk_navigator_units():
    assert BUDGET_PER_100K["delta"] == 5.0
    assert BUDGET_PER_100K["vega"] == 1200.0


def test_the_book_that_exposed_the_miscalibration_reads_off_neutral():
    # 14 SPX deltas on $215k NLV — the reading that used to pass as neutral.
    budget = budget_for(215_000)
    assert abs(14.0) > budget["delta"], (
        "a 14-delta book on $215k must breach the delta budget; the old "
        "0.30-per-unit band scored it as neutral")


def test_budget_scales_linearly_with_nlv_and_has_a_floor():
    assert budget_for(200_000)["delta"] == 2 * budget_for(100_000)["delta"]
    # Small accounts are floored at a quarter unit rather than scaling to zero.
    assert budget_for(1_000)["delta"] == budget_for(25_000)["delta"]


def test_harvest_book_requires_non_negative_theta():
    assert budget_for(100_000)["theta_min"] == 0.0
