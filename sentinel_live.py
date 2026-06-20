"""
sentinel_live.py — connected-mode runner for Sentinel.

Place this at the Forward-Vol-Scanner repo root (next to webapp.py), with
sentinel.py beside it. Then:

    python sentinel_live.py          # offline dry-run on FVS mock data (no TWS)
    python sentinel_live.py live     # connect to TWS, use your REAL positions

Live needs ib_insync installed and TWS/Gateway running with the API enabled
(127.0.0.1:7496). Edit SYMBOLS and SMSF_ACCOUNTS for your setup.

It reuses FVS end to end — build_context (regime + surface), list_accounts,
fetch_positions, book_greeks — and only adds Sentinel's decision layer on top.
"""
from __future__ import annotations

import sys
from datetime import timedelta

from core.context import build_context            # FVS: regime + slices in one call
from core.events import trading_today             # FVS: NY-anchored date
from core.surface import term_stats               # FVS: term/skew verdict
from core.ib_client import with_ib                # FVS: short-lived TWS connection
from portfolio.accounts import MOCK_ACCOUNTS, list_accounts
from portfolio.book import book_greeks, fetch_positions

import sentinel as S

# --- your setup ------------------------------------------------------------
SYMBOLS = ["SPX"]                   # underlyings you carry positions in
SMSF_ACCOUNTS = {"REPLACE-SMSF"}   # your real IBKR SMSF (cash) account id(s)
# ---------------------------------------------------------------------------


def _mock_positions(spot: float) -> list[dict]:
    """A short strangle ~5% wide, ~21 DTE — gives the dry-run real Greeks."""
    today = trading_today()
    f = (today + timedelta(days=21)).strftime("%Y%m%d")
    return [{"cp": "P", "strike": round(spot * 0.95), "expiry": f, "qty": -1, "conId": 0},
            {"cp": "C", "strike": round(spot * 1.05), "expiry": f, "qty": -1, "conId": 0}]


def run(mode: str) -> None:
    live = mode == "live"
    accts = with_ib(list_accounts) if live else MOCK_ACCOUNTS
    for symbol in SYMBOLS:
        ctx = build_context(symbol, mode)
        reg = S.RegimeView.from_fvs({**ctx.regime, "symbol": symbol},
                                    term_stats(ctx.slices))

        if live:
            pos_by = with_ib(lambda ib: {a["account"]: fetch_positions(ib, symbol, a["account"])
                                         for a in accts})
        else:
            mp = _mock_positions(ctx.spot)
            pos_by = {a["account"]: mp for a in accts}

        books = []
        for a in accts:
            bg = book_greeks(ctx, pos_by.get(a["account"], []))
            is_smsf = a["account"] in SMSF_ACCOUNTS or (not live and a is accts[-1])
            books.append(S.BookView.from_fvs(
                a, bg, label=a["account"],
                pool="investing" if is_smsf else "trading",
                smsf_eu_cash_block=is_smsf and symbol.upper() in S.EU_CASH_INDEX))

        print(S.render(S.advise(reg, books)))


if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else "mock")
