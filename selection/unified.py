"""Campaign Engine selector entry point."""
from selection.campaign_ranker import rank_campaign


def campaign_shortlist(ctx, intent: str, account: str | None, nlv: float | None) -> dict:
    return rank_campaign(ctx, intent, account, nlv, include_all=False)
