"""Expanded Campaign Engine list, using the same ranker as the shortlist."""
from selection.campaign_ranker import rank_campaign


def strategy_lab(ctx, intent: str, account: str | None, nlv: float | None) -> dict:
    return rank_campaign(ctx, intent, account, nlv, include_all=True)
