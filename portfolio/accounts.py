"""Managed-accounts discovery + NLV, cached 10 min (cheap TWS calls)."""
from __future__ import annotations
from core.ib_client import TTLCache

ACCT_CACHE = TTLCache(600)

MOCK_ACCOUNTS = [{"account": "MOCK-A", "nlv": 250_000.0},
                 {"account": "MOCK-B", "nlv": 100_000.0}]


def list_accounts(ib) -> list[dict]:
    hit = ACCT_CACHE.get("accts")
    if hit:
        return hit
    out = []
    rows = ib.accountSummary()           # all accounts, one request
    for a in ib.managedAccounts():
        nlv = next((float(r.value) for r in rows
                    if r.account == a and r.tag == "NetLiquidation"), None)
        out.append({"account": a, "nlv": nlv})
    ACCT_CACHE.put("accts", out)
    return out
