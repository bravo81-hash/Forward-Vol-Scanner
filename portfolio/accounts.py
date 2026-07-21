"""Managed-accounts discovery + NLV, cached 10 min (cheap TWS calls)."""
from __future__ import annotations
from core.ib_client import TTLCache

ACCT_CACHE = TTLCache(600)

MOCK_ACCOUNTS = [{"account": "MOCK-A", "nlv": 250_000.0,
                  "available_funds": 125_000.0, "cash": 75_000.0},
                 {"account": "MOCK-B", "nlv": 100_000.0,
                  "available_funds": 50_000.0, "cash": 30_000.0}]


def list_accounts(ib) -> list[dict]:
    managed = tuple(ib.managedAccounts())
    cache_key = ("accts", managed)
    hit = ACCT_CACHE.get(cache_key)
    if hit:
        return hit
    out = []
    rows = ib.accountSummary()           # all accounts, one request
    def value(account: str, tag: str) -> float | None:
        try:
            return next(float(r.value) for r in rows
                        if r.account == account and r.tag == tag)
        except (StopIteration, TypeError, ValueError):
            return None

    for a in managed:
        out.append({"account": a,
                    "nlv": value(a, "NetLiquidation"),
                    "available_funds": value(a, "AvailableFunds"),
                    "cash": value(a, "TotalCashValue")})
    ACCT_CACHE.put(cache_key, out)
    return out
