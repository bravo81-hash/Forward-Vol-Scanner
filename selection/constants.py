"""Shared selection constants derived from the strategy registries.

These sets used to be hand-written literals repeated in ranker.py,
campaign_ranker.py and last_hour.py. They had already drifted apart, and a
new multi-expiry family would have been silently tradeable in a cash
account. They are now derived from each Strategy's `multi_expiry`
declaration, so adding a family is enough to gate it everywhere.
"""
from __future__ import annotations

from strategies import REGISTRY
from strategies.last_hour import LAST_HOUR_REGISTRY


def _multi(registry: dict) -> frozenset[str]:
    return frozenset(k for k, s in registry.items() if getattr(s, "multi_expiry", False))


#: Campaign/research families that span more than one expiry.
MULTI_EXPIRY = _multi(REGISTRY)

#: Last-hour desk families that span more than one expiry.
LAST_HOUR_MULTI_EXPIRY = _multi(LAST_HOUR_REGISTRY)

#: Every multi-expiry family key, whichever desk it belongs to.
ALL_MULTI_EXPIRY = MULTI_EXPIRY | LAST_HOUR_MULTI_EXPIRY
