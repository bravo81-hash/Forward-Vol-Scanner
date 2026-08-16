#!/usr/bin/env python3
"""Read-only preflight against a running TWS.

Checks the code paths that could only be exercised with mock data during the
optimisation pass. It qualifies contracts and reads account values; it never
places an order, not even a whatIf, so it is safe to run against the live
account — though paper (port 7497) is the sensible first run.

    python tools/tws_preflight.py                 # defaults to SPX
    python tools/tws_preflight.py --symbol RUT --port 7497

What it answers:
  1. Does TWS actually populate `minTick` on a qualified option contract?
     The staging tick now comes from the contract instead of a hardcoded
     0.05, and this is the assumption that change rests on.
  2. What limit price would a real card round to, old rule vs new?
  3. Do the account rows carry AvailableFunds, so Gate S can do the
     affordability check?
  4. Does a live context build, and does the surface look sane?
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.chain import SURFACE_CFG
from core.context import build_context
from core.ib_client import with_ib
from core.surface import term_stats
from execution.stage import DEFAULT_TICK, _round_tick, combo_tick
from portfolio.accounts import list_accounts
from selection.ranker import shortlist

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "


def line(status: str, text: str) -> None:
    print(f"[{status}] {text}")


def check_accounts(ib) -> bool:
    rows = list_accounts(ib)
    if not rows:
        line(FAIL, "no managed accounts returned")
        return False
    good = True
    for row in rows:
        funds = row.get("available_funds")
        nlv = row.get("nlv")
        if funds is None:
            line(WARN, f"{row['account']}: no AvailableFunds — Gate S will "
                       f"report CASH UNKNOWN for this account")
            good = False
        else:
            line(OK, f"{row['account']}: NLV ${nlv:,.0f}, available ${funds:,.0f}")
    return good


def check_ticks(ib, symbol: str) -> bool:
    """Qualify the legs of a real candidate and report each leg's minTick."""
    from ib_insync import Option

    ctx = build_context(symbol, "live")
    out = shortlist(ctx)
    cards = out.get("cards") or []
    if not cards:
        line(WARN, f"{symbol}: ranker produced no cards; cannot check ticks")
        return False
    card = cards[0]
    legs = card["legs_raw"]
    _st, _exch, tc, _idx = SURFACE_CFG.get(symbol, ("STK", "SMART", symbol, False))
    tc = next((leg.get("trading_class") for leg in legs if leg.get("trading_class")), tc)

    contracts = [Option(symbol, leg["expiry"].replace("-", ""), leg["strike"],
                        leg["cp"], "SMART", tradingClass=tc, currency="USD")
                 for leg in legs]
    ib.qualifyContracts(*contracts)

    if any(not c.conId for c in contracts):
        line(FAIL, f"{symbol}: leg qualification failed — staging would raise")
        return False
    line(OK, f"{symbol}: all {len(contracts)} legs qualified ({card['strategy']})")

    ticks = []
    for leg, contract in zip(legs, contracts, strict=True):
        tick = getattr(contract, "minTick", None)
        ticks.append(tick)
        label = f"{leg['expiry']} {leg['strike']:g}{leg['cp']}"
        if tick in (None, 0):
            line(WARN, f"  {label}: minTick not populated")
        else:
            line(OK, f"  {label}: minTick {tick}")

    if not any(t for t in ticks if t):
        line(WARN, "no leg reported a minTick — combo_tick falls back to "
                   f"{DEFAULT_TICK}, i.e. the old behaviour. Not a regression, "
                   "but the tick fix buys nothing on this contract.")
        return False

    tick = combo_tick(contracts)
    net_mid = float(card.get("net_mid") or 0.0)
    new_px = _round_tick(abs(net_mid), tick)
    old_px = _round_tick(abs(net_mid), DEFAULT_TICK)
    line(OK, f"combo tick {tick} (was hardcoded {DEFAULT_TICK})")
    line(OK, f"net_mid {abs(net_mid):.4f} -> limit {new_px} (old rule: {old_px})")
    if abs(new_px - old_px) > 1e-9:
        line(WARN, "the two rules disagree on this structure — eyeball the "
                   "limit in the TWS order book on your first staged trade")
    return True


def check_surface(ib, symbol: str) -> bool:
    ctx = build_context(symbol, "live")
    stats = term_stats(ctx.slices)
    if not ctx.slices:
        line(FAIL, f"{symbol}: no expiry slices built")
        return False
    line(OK, f"{symbol}: spot {ctx.spot:.2f}, {len(ctx.slices)} expiries, "
             f"term {stats.get('verdict', '?')}, vrp_fwd "
             f"{ctx.regime.get('vrp_fwd', float('nan')):.2f}v")
    if not ctx.data.get("fresh", True):
        line(WARN, f"{symbol}: market data flagged stale — is the market open?")
    dtes = [s.dte for s in ctx.slices]
    line(OK, f"{symbol}: listed DTEs {sorted(dtes)[:12]}")
    if not any(14 <= d <= 18 for d in dtes):
        line(WARN, "no listed 14-18 DTE expiry this week — TimeEdge will use "
                   "the tolerance fallback and say so on the card")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbol", default="SPX")
    ap.add_argument("--skip-ticks", action="store_true",
                    help="skip contract qualification (fewer TWS requests)")
    args = ap.parse_args()

    print(f"TWS preflight — {args.symbol} — read-only, no orders placed\n")

    def run(ib):
        results = {"accounts": check_accounts(ib)}
        print()
        results["surface"] = check_surface(ib, args.symbol)
        print()
        if not args.skip_ticks:
            results["ticks"] = check_ticks(ib, args.symbol)
        return results

    try:
        results = with_ib(run)
    except Exception as exc:  # noqa: BLE001
        line(FAIL, f"could not reach TWS: {type(exc).__name__}: {exc}")
        print("\nCheck TWS is running, API access is enabled, and the port "
              "matches (7496 live / 7497 paper).")
        return 2

    print()
    failed = [name for name, ok in results.items() if not ok]
    if failed:
        print(f"preflight finished with warnings in: {', '.join(failed)}")
        print("None of these block staging — they tell you which checks to "
              "eyeball on the first real order.")
        return 1
    print("preflight clean — every live-only path behaved as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
