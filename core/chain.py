"""Per-expiry surface builder.

LIVE (TWS, pacing-aware — see ib_client):
  pass 1: quote ATM C+P per expiry  -> ATM IV, spread, OI
  pass 2: quote 25-delta put/call strikes (computed from pass-1 IV)
  Total lines ~= 4 x n_expiries (8 expiries -> 32), batched + cancelled.
  Result cached 5 min per symbol.

MOCK: synthetic skewed surface, no TWS required.
"""
from __future__ import annotations
import math
from datetime import date, datetime, timedelta

from .ib_client import CHAIN_CACHE, PARAMS_CACHE, quote_many
from .models import Slice

SCAN_DTE = (5, 50)

SURFACE_CFG = {   # symbol: (secType, exchange, tradingClass, is_index)
    "SPX": ("IND", "CBOE", "SPXW", True),
    "RUT": ("IND", "RUSSELL", "RUTW", True),
    "NDX": ("IND", "NASDAQ", "NDXP", True),
    "SPY": ("STK", "SMART", "SPY", False),
    "QQQ": ("STK", "SMART", "QQQ", False),
    "IWM": ("STK", "SMART", "IWM", False),
}


def k25(spot: float, iv: float, t_yr: float, cp: str) -> float:
    """Strike at ~|delta|=.25 from ATM vol (z=.675)."""
    z = 0.675 * iv * math.sqrt(max(t_yr, 1e-4))
    return spot * math.exp(-z) if cp == "P" else spot * math.exp(z)


# ------------------------------------------------------------------ LIVE ----
def build_chain_live(ib, symbol: str, today: date) -> tuple[float, list[Slice], list[float]]:
    cached = CHAIN_CACHE.get(symbol)
    if cached:
        return cached
    from ib_insync import Index, Option, Stock
    st, exch, tc, is_idx = SURFACE_CFG[symbol]
    und = (Index(symbol, exch, "USD") if is_idx else Stock(symbol, "SMART", "USD"))
    ib.qualifyContracts(und)

    q = quote_many(ib, [und], want_greeks=False, timeout=4)
    spot = (q.get(und.conId) or {}).get("mid")
    if not spot:
        t = ib.reqMktData(und, "", snapshot=False)
        ib.sleep(2)
        spot = t.last or t.close
        ib.cancelMktData(und)
    if not spot:
        raise RuntimeError(f"{symbol}: no spot")

    pkey = ("params", symbol)
    chain = PARAMS_CACHE.get(pkey)
    if chain is None:
        chains = ib.reqSecDefOptParams(und.symbol, "", und.secType, und.conId)
        chain = next((c for c in chains if c.tradingClass == tc), chains[0])
        PARAMS_CACHE.put(pkey, chain)

    expiries = []
    for e in sorted(chain.expirations):
        d = datetime.strptime(e, "%Y%m%d").date()
        if SCAN_DTE[0] <= (d - today).days <= SCAN_DTE[1] and d.weekday() == 4:
            expiries.append(d)            # Fridays only — keeps lines low
    strikes = sorted(k for k in chain.strikes if 0.8 * spot < k < 1.2 * spot)

    def snap(x):
        return min(strikes, key=lambda s: abs(s - x))

    opt_exch = "SMART"
    # pass 1: ATM C/P
    p1 = []
    for d in expiries:
        ks = snap(spot)
        for cp in ("C", "P"):
            p1.append((d, "atm", cp, ks,
                       Option(symbol, d.strftime("%Y%m%d"), ks, cp, opt_exch,
                              tradingClass=chain.tradingClass, currency="USD")))
    ib.qualifyContracts(*[x[4] for x in p1])
    q1 = quote_many(ib, [x[4] for x in p1 if x[4].conId], fields="100,101")

    atm: dict[date, dict] = {}
    for d, _, cp, ks, c in p1:
        r = q1.get(c.conId) or {}
        a = atm.setdefault(d, {"strike": ks, "ivs": [], "spr": [], "oi": 0})
        if r.get("iv"):
            a["ivs"].append(r["iv"])
        if r.get("bid") and r.get("ask") and r.get("mid"):
            a["spr"].append((r["ask"] - r["bid"]) / r["mid"])
        a["oi"] += r.get("oi") or 0

    # pass 2: 25-delta wings
    p2 = []
    for d, a in atm.items():
        if not a["ivs"]:
            continue
        iv = sum(a["ivs"]) / len(a["ivs"])
        t = (d - today).days / 365.0
        for cp in ("P", "C"):
            ks = snap(k25(spot, iv, t, cp))
            p2.append((d, cp, ks,
                       Option(symbol, d.strftime("%Y%m%d"), ks, cp, opt_exch,
                              tradingClass=chain.tradingClass, currency="USD")))
    ib.qualifyContracts(*[x[3] for x in p2])
    q2 = quote_many(ib, [x[3] for x in p2 if x[3].conId])

    slices = []
    for d, a in sorted(atm.items()):
        if not a["ivs"]:
            continue
        sl = Slice(expiry=d, dte=(d - today).days, atm_strike=a["strike"],
                   atm_iv=sum(a["ivs"]) / len(a["ivs"]),
                   atm_spread_pct=sum(a["spr"]) / len(a["spr"]) if a["spr"] else 0.10,
                   oi_atm=a["oi"])
        for dd, cp, ks, c in p2:
            if dd != d:
                continue
            iv = (q2.get(c.conId) or {}).get("iv")
            if cp == "P":
                sl.put25_iv, sl.put25_strike = iv or 0.0, ks
            else:
                sl.call25_iv, sl.call25_strike = iv or 0.0, ks
        slices.append(sl)
    out = (float(spot), slices, strikes)
    CHAIN_CACHE.put(symbol, out)
    return out


# ------------------------------------------------------------------ MOCK ----
MOCK = {  # base 30d IV, term slope, skew (rr25 vol pts at 30d), event kink
    "SPX": (0.14, 0.020, 4.5, True), "SPY": (0.14, 0.020, 4.5, True),
    "QQQ": (0.18, 0.022, 3.5, True), "NDX": (0.18, 0.022, 3.5, True),
    "RUT": (0.20, 0.015, 3.0, False), "IWM": (0.20, 0.015, 3.0, False),
}
MOCK_SPOT = {"SPX": 6000.0, "NDX": 21500.0, "RUT": 2300.0,
             "SPY": 600.0, "QQQ": 525.0, "IWM": 230.0}


def build_chain_mock(symbol: str, today: date) -> tuple[float, list[Slice], list[float]]:
    base, slope, rr, kink = MOCK[symbol]
    spot = MOCK_SPOT[symbol]
    step = max(round(spot * 0.0042, 0), 0.5) if spot < 1000 else 5.0
    strikes = [round(spot * 0.8 + i * step, 1) for i in range(int(spot * 0.4 / step) + 1)]

    def snap(x):
        return min(strikes, key=lambda s: abs(s - x))

    from .events import fomc_within
    slices = []
    d = today
    while (d - today).days <= SCAN_DTE[1]:
        d += timedelta(days=1)
        if d.weekday() != 4 or (d - today).days < SCAN_DTE[0]:
            continue
        dte = (d - today).days
        iv = base + slope * math.sqrt(dte / 30.0)
        if kink and fomc_within(d, today):
            iv += 0.015 * math.exp(-max(0, dte - 5) / 12.0)
        t = dte / 365.0
        half_rr = rr / 200.0
        slices.append(Slice(
            expiry=d, dte=dte, atm_strike=snap(spot), atm_iv=round(iv, 4),
            put25_iv=round(iv + half_rr, 4), call25_iv=round(iv - half_rr, 4),
            put25_strike=snap(k25(spot, iv, t, "P")),
            call25_strike=snap(k25(spot, iv, t, "C")),
            atm_spread_pct=0.04 if symbol in ("SPX", "SPY", "QQQ") else 0.09,
            oi_atm=8000))
    return spot, slices, strikes


def iv_at(slc: Slice, strike: float) -> float:
    """Skew-aware IV at a strike: linear between 25d wings and ATM, flat beyond."""
    if strike <= slc.atm_strike and slc.put25_iv and slc.put25_strike < slc.atm_strike:
        f = (slc.atm_strike - strike) / (slc.atm_strike - slc.put25_strike)
        return slc.atm_iv + min(f, 1.6) * (slc.put25_iv - slc.atm_iv)
    if strike > slc.atm_strike and slc.call25_iv and slc.call25_strike > slc.atm_strike:
        f = (strike - slc.atm_strike) / (slc.call25_strike - slc.atm_strike)
        return slc.atm_iv + min(f, 1.6) * (slc.call25_iv - slc.atm_iv)
    return slc.atm_iv
