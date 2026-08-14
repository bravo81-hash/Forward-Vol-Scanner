"""Context + bars for the equity LEAPS engine.

Three problems this module exists to solve, all found the hard way:

1. core.chain.SCAN_DTE caps the default yfinance context at 85 DTE. A
   months-long hold against that surface silently returns a four-week
   structure - a card that looks right and is wrong.

2. core.yf_client populates ctx.strikes with three points per expiry (ATM and
   the two 25-delta wings), which is all the forward-vol work needs. A ZEBRA
   solver handed three strikes is not solving anything - on NVDA it left one
   candidate below spot and reported the only available strike as chosen.

3. yfinance throttles, and separately serves PLACEHOLDER implied volatility on
   long-dated options. Measured on NVDA: healthy chains (84 calls, 68 shared
   strikes, 30-460 span) where the ATM call IV read 1e-05 and the ATM put
   4.98e-04 at 217d, 307d and 399d. core.yf_client rejects such slices, quite
   correctly, and then reports "0 expiries" - a statement about the wrong
   thing. So slices are built here with IV solved from mid prices instead.

So: expiries are probed once with retry, results are cached in-process, and a
tenor shortfall degrades to a card carrying a TENOR BLOCK rather than raising.
"""
from __future__ import annotations

import time
from datetime import date, datetime

from core.models import Context

WINDOWS = {"short": (3, 45), "medium": (14, 120), "long": (90, 500)}
TARGETS = {"short": 14, "medium": 45, "long": 300}

STRIKE_LO, STRIKE_HI = 0.45, 1.35
# A ZEBRA long leg solves to roughly 0.8x spot, so the live strike band has to
# reach well past core.chain's default +/-20%.
LIVE_STRIKE_BAND = (0.45, 1.35)

_LIVE_ERRORS: dict = {}
MIN_GRID = 8

RETRIES = 3
BACKOFF = 1.5
CACHE_TTL = 900

# yfinance charges one HTTP round trip per expiry chain. build_context_yf
# walks every expiry inside its dte_range, so a wide LEAPS window asked for
# up to 16 chains per analysis and got throttled into empty responses - which
# then surfaced as "0 expiries", a message about the wrong thing entirely.
# Bracketing the range around the few expiries actually wanted cuts that to
# MAX_SLICES calls.
MAX_SLICES = 3

_CACHE = {}


def window_for(hold: str) -> tuple[int, int]:
    return WINDOWS.get(hold, WINDOWS["medium"])


def _cached(key, fn):
    hit = _CACHE.get(key)
    now = time.time()
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    val = fn()
    _CACHE[key] = (now, val)
    return val


def clear_cache() -> None:
    _CACHE.clear()


def _retry(fn, what: str):
    """Retry through yfinance throttling. An empty response counts as failure."""
    last, delay = None, BACKOFF
    for attempt in range(RETRIES):
        try:
            out = fn()
            if out:
                return out
            last = "%s: empty response (attempt %d)" % (what, attempt + 1)
        except Exception as exc:                     # noqa: BLE001
            last = "%s: %s: %s" % (what, type(exc).__name__, exc)
        if attempt < RETRIES - 1:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(last or ("%s: no data" % what))


def probe_expiries(symbol: str, today: date) -> list[tuple[str, int]]:
    """(expiry string, dte) for every listed expiry. Cached and retried."""
    def fetch():
        import yfinance as yf
        return list(yf.Ticker(symbol).options or [])

    raw = _cached(("exp", symbol), lambda: _retry(fetch, "%s expiries" % symbol))
    out = []
    for e in raw:
        try:
            d = datetime.strptime(e, "%Y-%m-%d").date()
        except ValueError:
            continue
        out.append((e, (d - today).days))
    return sorted(out, key=lambda x: x[1])


def listed_strikes(symbol: str, expiry: date, spot: float) -> list[float]:
    """Real listed strikes for one expiry, wide enough for a deep ITM solve."""
    def fetch():
        import yfinance as yf
        ch = yf.Ticker(symbol).option_chain(expiry.strftime("%Y-%m-%d"))
        return sorted({float(k) for k in ch.calls["strike"].tolist()})

    ks = _cached(("strikes", symbol, expiry.isoformat()),
                 lambda: _retry(fetch, "%s %s strikes" % (symbol, expiry)))
    lo, hi = spot * STRIKE_LO, spot * STRIKE_HI
    return [k for k in ks if lo <= k <= hi]


def build_live(symbol: str, hold: str, today: date):
    """Context from IBKR/TWS. Real quotes, real IV, real strike ladder.

    Everything in core/iv_solve.py exists to work around Yahoo serving
    placeholder IV on LEAPS. TWS has none of that problem, so when it is
    reachable this path is strictly better: NBBO bid/ask instead of solved
    mids, and the option chain definition instead of a scraped table.
    """
    from core.context import build_context

    lo, hi = window_for(hold)
    ctx = build_context(symbol, "live", today=today,
                        dte_range=(lo, hi), strike_band=LIVE_STRIKE_BAND)
    ctx.data["chain_source"] = "IBKR TWS"
    return ctx


def build(symbol: str, hold: str = "medium", *, today=None, source: str = "auto"):
    """Return (context, daily bars).

    Never raises for a tenor shortfall - builds the widest usable window and
    records the shortfall in ctx.data so Gate E raises a TENOR BLOCK on the
    card. Only a genuine data failure raises.
    """
    from core.events import trading_today
    from core.stock_data import histories_yf

    today = today or trading_today()

    # TWS first when it is reachable. Falling back rather than failing means a
    # closed TWS degrades the data quality instead of the feature.
    if source in ("auto", "live"):
        try:
            ctx = _cached(("live", symbol, hold),
                          lambda: build_live(symbol, hold, today))
            bars = histories_yf([symbol], period="2y").get(symbol, [])
            ctx.data["strikes_below_spot"] = sum(1 for k in ctx.strikes
                                                 if k < ctx.spot)
            return ctx, bars
        except Exception as exc:                     # noqa: BLE001
            if source == "live":
                raise
            _LIVE_ERRORS[symbol] = "%s: %s" % (type(exc).__name__, exc)

    lo, hi = window_for(hold)
    expiries = probe_expiries(symbol, today)
    in_window = [e for e in expiries if lo <= e[1] <= hi]
    shortfall = None

    if len(in_window) < 2:
        widest = [e for e in expiries if e[1] >= 5]
        if len(widest) < 2:
            raise RuntimeError("%s: only %d expiries listed, none usable"
                               % (symbol, len(expiries)))
        in_window = widest
        shortfall = {"requested": list(window_for(hold)),
                     "available_dte": [widest[0][1], widest[-1][1]],
                     "count_in_window": 0}

    # Pick the few expiries nearest the target and bracket the range tightly
    # around them, so build_context_yf fetches MAX_SLICES chains rather than
    # every listed expiry in the window.
    target = TARGETS.get(hold, 45)
    chosen = sorted(sorted(in_window, key=lambda e: abs(e[1] - target))[:MAX_SLICES],
                    key=lambda e: e[1])
    lo, hi = chosen[0][1] - 1, chosen[-1][1] + 1

    ctx = _cached(("ctx", symbol, hold, lo, hi),
                  lambda: _retry(
                      lambda: _context(symbol, today, (lo, hi)),
                      "%s chains %d-%dd" % (symbol, lo, hi)))
    bars = histories_yf([symbol], period="2y").get(symbol, [])

    if shortfall:
        ctx.data["tenor_shortfall"] = shortfall

    slc = ctx.slice_near(TARGETS.get(hold, 45))
    if slc is not None:
        try:
            grid = listed_strikes(symbol, slc.expiry, ctx.spot)
            if len(grid) >= MIN_GRID:
                ctx.strikes = sorted(set(ctx.strikes) | set(grid))
                ctx.data["strike_grid"] = len(grid)
            else:
                ctx.data["strike_grid_thin"] = len(grid)
        except Exception as exc:                     # noqa: BLE001
            ctx.data["strike_grid_error"] = "%s: %s" % (type(exc).__name__, exc)

    ctx.data["strikes_below_spot"] = sum(1 for k in ctx.strikes if k < ctx.spot)
    ctx.data["expiries_listed"] = len(expiries)
    ctx.data["chains_fetched"] = len(chosen)
    ctx.data["chain_source"] = "yfinance"
    if symbol in _LIVE_ERRORS:
        ctx.data["live_unavailable"] = _LIVE_ERRORS[symbol]
    return ctx, bars



def _context(symbol: str, today: date, window: tuple[int, int]):
    """build_context_yf, but with slice IVs solved from mid prices.

    Yahoo's impliedVolatility column is unusable on LEAPS, so the frames are
    repaired before core.yf_client sees them. Everything else - ATM selection,
    the 25-delta wings, spread and open interest - is yf_client's own logic,
    reused unchanged.
    """
    import core.yf_client as yfc
    from core.iv_solve import repair_iv
    from core.pricing import q_for

    original = yfc._slice_from_chain
    qdiv = q_for(symbol)
    curves: dict = {}

    def patched(expiry, dte, spot, calls, puts, q):
        t = max(dte, 1) / 365.0
        rc = repair_iv(calls, spot, t, "C", q=qdiv)
        rp = repair_iv(puts, spot, t, "P", q=qdiv)
        # Keep the whole solved call curve. core.chain.iv_at interpolates from
        # only three anchors (ATM and the two 25-delta wings), which distorts
        # extrinsic away from those points - and the ZEBRA solver lives deep
        # ITM, far from all three.
        try:
            curves[expiry.isoformat()] = {
                float(r["strike"]): float(r["impliedVolatility"])
                for _, r in rc.iterrows()
                if r.get("impliedVolatility") and float(r["impliedVolatility"]) > 0}
        except Exception:                            # noqa: BLE001
            pass
        return original(expiry, dte, spot, rc, rp, q)

    yfc._slice_from_chain = patched
    try:
        ctx = yfc.build_context_yf(symbol, today=today, dte_range=window)
    finally:
        yfc._slice_from_chain = original
    ctx.data["iv_source"] = "solved from mid where quoted IV was implausible"
    ctx.data["iv_curve"] = curves
    return ctx
