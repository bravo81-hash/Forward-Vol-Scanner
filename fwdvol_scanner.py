#!/usr/bin/env python3
"""
fwdvol_scanner.py — Forward-vol calendar/diagonal pair scanner (IBKR / ib_insync)

Enumerates expiry pairs per surface (SPX/NDX/RUT), pulls ATM IV per expiry from
live option quotes, computes forward vol for every (front, back) pair, and ranks
by edge score = front_IV - forward_vol (vol points). Flags event placement.

Forward vol:  sigma_fwd = sqrt( (s2^2*T2 - s1^2*T1) / (T2 - T1) ),  T in years.

Usage:
    python fwdvol_scanner.py                 # live scan via TWS
    python fwdvol_scanner.py --dry-run       # synthetic surface, no TWS needed
    python fwdvol_scanner.py --host 192.168.0.185 --port 7496 --underlyings SPX RUT

Output: one ranked table per surface + a one-line dashboard verdict per surface
(paste the best score into the TE Console manual notes if desired).

Requires: ib_insync, pandas, numpy. Market data subscriptions for index options.
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from core.events import trading_today

import numpy as np
import pandas as pd

# ----------------------------------------------------------------- CONFIG ---

DEFAULT_HOST = "127.0.0.1"       # TWS on this machine
DEFAULT_PORT = 7496              # 7497 = paper
DEFAULT_CLIENT_ID = 31

# DTE bands (entry-day, calendar days)
FRONT_DTE = (12, 21)
BACK_DTE = (26, 45)
MIN_GAP_DAYS = 7                 # min separation front -> back
SCAN_DTE = (10, 50)              # expiries to quote at all

# Edit each session / week: known binary events
EVENTS: dict[str, str] = {
    "2026-06-17": "FOMC",
    "2026-07-14": "CPI",
    "2026-07-29": "FOMC",
}

@dataclass
class Surface:
    symbol: str
    sec_type: str = "IND"
    exchange: str = "CBOE"
    currency: str = "USD"
    trading_class: str | None = None     # SPXW for weeklies
    opt_exchange: str = "SMART"

SURFACES: dict[str, Surface] = {
    "SPX": Surface("SPX", trading_class="SPXW"),
    "RUT": Surface("RUT", exchange="RUSSELL", trading_class="RUTW"),
    "SPY": Surface("SPY", sec_type="STK", exchange="SMART"),
    "QQQ": Surface("QQQ", sec_type="STK", exchange="SMART"),
    "IWM": Surface("IWM", sec_type="STK", exchange="SMART"),
}

QUOTE_TIMEOUT_S = 8.0            # wait for modelGreeks per batch
BATCH_SIZE = 16                  # mkt data lines per batch (stay under limits)

# ------------------------------------------------------------------- MATH ---

def forward_vol(iv1: float, t1: float, iv2: float, t2: float) -> float:
    """Forward vol between T1 and T2. IVs as decimals, T in years. NaN if inverted."""
    if t2 <= t1:
        return float("nan")
    var = (iv2 ** 2 * t2 - iv1 ** 2 * t1) / (t2 - t1)
    return math.sqrt(var) if var > 0 else float("nan")


def event_flag(front_exp: date, back_exp: date, today: date) -> str:
    """Classify event placement relative to the pair."""
    flags = []
    for ds, name in EVENTS.items():
        d = datetime.strptime(ds, "%Y-%m-%d").date()
        if today < d <= front_exp:
            flags.append(f"{name} IN FRONT (harvest if rich)")
        elif front_exp < d <= back_exp:
            flags.append(f"{name} IN BACK ONLY (paying for event)")
    return "; ".join(flags) if flags else "clean"


def build_pairs(ivs: dict[date, float], today: date) -> pd.DataFrame:
    """ivs: expiry -> ATM IV (decimal). Returns ranked pair table."""
    rows = []
    exps = sorted(ivs)
    for fe in exps:
        fdte = (fe - today).days
        if not (FRONT_DTE[0] <= fdte <= FRONT_DTE[1]):
            continue
        for be in exps:
            bdte = (be - today).days
            if not (BACK_DTE[0] <= bdte <= BACK_DTE[1]):
                continue
            if (be - fe).days < MIN_GAP_DAYS:
                continue
            iv1, iv2 = ivs[fe], ivs[be]
            if not (np.isfinite(iv1) and np.isfinite(iv2)):
                continue
            fv = forward_vol(iv1, fdte / 365.0, iv2, bdte / 365.0)
            rows.append({
                "front": fe.isoformat(), "fDTE": fdte, "fIV%": round(iv1 * 100, 2),
                "back": be.isoformat(), "bDTE": bdte, "bIV%": round(iv2 * 100, 2),
                "fwdVol%": round(fv * 100, 2) if np.isfinite(fv) else np.nan,
                "edge(f-fwd)": round((iv1 - fv) * 100, 2) if np.isfinite(fv) else np.nan,
                "events": event_flag(fe, be, today),
            })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values("edge(f-fwd)", ascending=False, na_position="last").reset_index(drop=True)

# ------------------------------------------------------------ IBKR PLUMBING ---

def fetch_ivs_ib(ib, surf: Surface, today: date) -> tuple[float, float, str, dict[date, float]]:
    """Return (spot, atm_strike, trading_class, {expiry: ATM IV}) via live option quotes."""
    from ib_insync import Index, Option, Stock

    if surf.sec_type == "STK":
        und = Stock(surf.symbol, surf.exchange, surf.currency)
    else:
        und = Index(surf.symbol, surf.exchange, surf.currency)
    ib.qualifyContracts(und)

    # Spot
    t = ib.reqMktData(und, "", snapshot=False)
    ib.sleep(2.0)
    spot = next((x for x in (t.last, t.close, t.marketPrice()) if x and not math.isnan(x)), None)
    ib.cancelMktData(und)
    if spot is None:
        raise RuntimeError(f"{surf.symbol}: no spot")

    # Chain params
    chains = ib.reqSecDefOptParams(und.symbol, "", und.secType, und.conId)
    want_tc = surf.trading_class or surf.symbol   # ETFs: skip junk like "2SPY"
    cands = [c for c in chains if c.tradingClass == want_tc] or list(chains)
    chain = max(cands, key=lambda c: len(c.expirations))

    expiries = []
    for e in sorted(chain.expirations):
        d = datetime.strptime(e, "%Y%m%d").date()
        if SCAN_DTE[0] <= (d - today).days <= SCAN_DTE[1]:
            expiries.append(d)
    strikes = sorted(chain.strikes)
    if not expiries or not strikes:
        raise RuntimeError(f"{surf.symbol}: empty chain in scan window")
    atm = min(strikes, key=lambda k: abs(k - spot))

    # Build ATM call+put per expiry, quote in batches, read modelGreeks.impliedVol
    ivs: dict[date, float] = {}
    contracts = []
    for d in expiries:
        for right in ("C", "P"):
            contracts.append((d, Option(surf.symbol, d.strftime("%Y%m%d"), atm, right,
                                        surf.opt_exchange, tradingClass=chain.tradingClass,
                                        currency=surf.currency)))
    # qualifyContracts fills conId in-place and omits failures from its return
    # value, so filter the originals — zipping against the return misaligns.
    ib.qualifyContracts(*[c for _, c in contracts])
    qmap = [(d, c) for d, c in contracts if c.conId]

    per_exp: dict[date, list[float]] = {}
    for i in range(0, len(qmap), BATCH_SIZE):
        batch = qmap[i:i + BATCH_SIZE]
        tickers = [(d, ib.reqMktData(c, "", snapshot=False)) for d, c in batch]
        waited = 0.0
        while waited < QUOTE_TIMEOUT_S:
            ib.sleep(0.5)
            waited += 0.5
            if all(tk.modelGreeks and tk.modelGreeks.impliedVol for _, tk in tickers):
                break
        for d, tk in tickers:
            iv = tk.modelGreeks.impliedVol if tk.modelGreeks else None
            if iv and 0.01 < iv < 3.0:
                per_exp.setdefault(d, []).append(iv)
            ib.cancelMktData(tk.contract)

    for d, vals in per_exp.items():
        ivs[d] = float(np.mean(vals))
    return spot, atm, chain.tradingClass, ivs

# --------------------------------------------------------------- DRY RUN ---

def synthetic_surface(today: date, base_iv: float, slope: float, kink_evt: bool) -> dict[date, float]:
    """Weekly Friday expiries with sqrt-time term slope; optional event kink."""
    ivs = {}
    d = today
    while (d - today).days <= SCAN_DTE[1]:
        d += timedelta(days=1)
        if d.weekday() != 4:  # Fridays
            continue
        dte = (d - today).days
        if dte < SCAN_DTE[0]:
            continue
        iv = base_iv + slope * math.sqrt(dte / 30.0)
        if kink_evt:
            for ds in EVENTS:
                ed = datetime.strptime(ds, "%Y-%m-%d").date()
                if today < ed <= d:
                    iv += 0.015 * math.exp(-max(0, (d - ed).days) / 10.0)
        ivs[d] = round(iv, 4)
    return ivs

# ------------------------------------------------------------------- MAIN ---

def main() -> int:
    ap = argparse.ArgumentParser(description="Forward-vol pair scanner")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--client-id", type=int, default=DEFAULT_CLIENT_ID)
    ap.add_argument("--underlyings", nargs="+", default=list(SURFACES),
                    choices=list(SURFACES))
    ap.add_argument("--top", type=int, default=8, help="rows per surface")
    ap.add_argument("--dry-run", action="store_true", help="synthetic IVs, no TWS")
    args = ap.parse_args()

    today = trading_today()
    pd.set_option("display.width", 160)

    ib = None
    if not args.dry_run:
        from ib_insync import IB
        ib = IB()
        try:
            ib.connect(args.host, args.port, clientId=args.client_id, timeout=10)
        except Exception as e:
            print(f"TWS connect failed ({args.host}:{args.port}): {e}", file=sys.stderr)
            return 1

    synth = {  # base 30d IV, term slope, event kink on?
        "SPX": (0.14, 0.020, True),
        "RUT": (0.20, 0.015, False),
        "SPY": (0.14, 0.020, True),
        "QQQ": (0.18, 0.022, True),
        "IWM": (0.20, 0.015, False),
    }

    verdicts = []
    try:
        for sym in args.underlyings:
            surf = SURFACES[sym]
            if args.dry_run:
                spot = {"SPX": 6000.0, "RUT": 2300.0, "SPY": 600.0,
                        "QQQ": 525.0, "IWM": 230.0}[sym]
                ivs = synthetic_surface(today, *synth[sym])
            else:
                spot, _atm, _tc, ivs = fetch_ivs_ib(ib, surf, today)

            df = build_pairs(ivs, today)
            print(f"\n=== {sym}  spot {spot:.0f}  ({len(ivs)} expiries quoted, "
                  f"{len(df)} valid pairs) ===")
            if df.empty:
                print("  no pairs in DTE bands")
                verdicts.append((sym, None))
                continue
            print(df.head(args.top).to_string(index=True))
            best = df.iloc[0]
            verdicts.append((sym, best))
    finally:
        if ib is not None:
            ib.disconnect()

    print("\n--- DASHBOARD VERDICTS ---")
    for sym, best in verdicts:
        if best is None:
            print(f"{sym}: no valid pair")
        else:
            tag = ("CHEAP FWD" if best["edge(f-fwd)"] > 1.0
                   else "MARGINAL" if best["edge(f-fwd)"] > 0 else "NO CAL EDGE")
            print(f"{sym}: best edge {best['edge(f-fwd)']:+.2f} vol pts "
                  f"({best['front']}/{best['back']}) -> {tag} | {best['events']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
