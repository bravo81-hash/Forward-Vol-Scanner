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


class EquityDataError(RuntimeError):
    """The upstream response is incomplete enough that trading must stop."""


class EquityThrottleError(EquityDataError):
    """The upstream explicitly reported a pacing/rate-limit condition."""

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


def _is_throttle_error(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    response_status = getattr(response, "status_code", None)
    return (status == 429 or response_status == 429
            or type(exc).__name__ in {"YFRateLimitError", "RateLimitError"})


def window_for(hold: str) -> tuple[int, int]:
    return WINDOWS.get(hold, WINDOWS["medium"])


def _set_comparison_iv(ctx: Context, hold: str) -> None:
    """Compare RV with ATM IV at the requested hold, not an extrapolated 30 DTE."""
    slc = ctx.slice_near(TARGETS.get(hold, 45))
    if slc is None or not slc.atm_iv:
        ctx.regime["iv30"] = None
        ctx.data["iv_comparison_dte"] = None
        return
    ctx.regime["iv30"] = slc.atm_iv * 100.0
    ctx.data["iv_comparison_dte"] = slc.dte
    ctx.data["iv_comparison_metric"] = "ATM IV at requested hold tenor"


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
    last, last_exc, delay = None, None, BACKOFF
    for attempt in range(RETRIES):
        try:
            out = fn()
            if out:
                return out
            last = f"{what}: empty response (attempt {attempt + 1})"
            last_exc = EquityDataError(last)
        except EquityThrottleError as exc:
            last_exc = exc
            last = str(exc)
        except Exception as exc:                     # noqa: BLE001
            if _is_throttle_error(exc):
                last_exc = EquityThrottleError(
                    f"{what}: upstream rate limit ({type(exc).__name__})")
            else:
                last_exc = EquityDataError(
                    f"{what}: {type(exc).__name__}: {exc}")
            last = str(last_exc)
        if attempt < RETRIES - 1:
            time.sleep(delay)
            delay *= 2
    raise last_exc or EquityDataError(last or (f"{what}: no data"))


def probe_expiries(symbol: str, today: date) -> list[tuple[str, int]]:
    """(expiry string, dte) for every listed expiry. Cached and retried."""
    def fetch():
        import yfinance as yf
        return list(yf.Ticker(symbol).options or [])

    raw = _cached(("exp", symbol), lambda: _retry(fetch, f"{symbol} expiries"))
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
                 lambda: _retry(fetch, f"{symbol} {expiry} strikes"))
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
    ctx.data["volatility_inputs_verified"] = (
        ctx.data.get("surface_source") == "live TWS IV")
    return ctx


def build(symbol: str, hold: str = "medium", *, today=None, source: str = "auto"):
    """Return (context, daily bars).

    Never raises for a tenor shortfall - builds the widest usable window and
    records the shortfall in ctx.data so Gate E raises a TENOR BLOCK on the
    card. Only a genuine data failure raises.
    """
    from core.events import trading_today
    from core.stock_data import histories_yf

    source = source.lower().strip()
    if source not in {"auto", "live", "yf"}:
        raise ValueError("source must be one of auto, live or yf")
    today = today or trading_today()

    # TWS first when it is reachable. Falling back rather than failing means a
    # closed TWS degrades the data quality instead of the feature.
    if source in ("auto", "live"):
        try:
            ctx = _cached(("live", symbol, hold, today.isoformat()),
                          lambda: build_live(symbol, hold, today))
            bars = histories_yf([symbol], period="2y").get(symbol, [])
            _set_comparison_iv(ctx, hold)
            ctx.data["strikes_below_spot"] = sum(1 for k in ctx.strikes
                                                 if k < ctx.spot)
            _LIVE_ERRORS.pop(symbol, None)
            return ctx, bars
        except Exception as exc:                     # noqa: BLE001
            if source == "live":
                raise
            _LIVE_ERRORS[symbol] = f"{type(exc).__name__}: {exc}"

    lo, hi = window_for(hold)
    expiries = probe_expiries(symbol, today)
    in_window = [e for e in expiries if lo <= e[1] <= hi]
    shortfall = None

    if not in_window:
        widest = [e for e in expiries if e[1] >= 5]
        if not widest:
            raise RuntimeError(
                f"{symbol}: only {len(expiries)} expiries listed, none usable")
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

    chosen_expiries = tuple(e[0] for e in chosen)
    ctx = _cached(("ctx", symbol, hold, lo, hi, chosen_expiries),
                  lambda: _retry(
                      lambda: _context(symbol, today, (lo, hi), chosen_expiries),
                      f"{symbol} chains {lo}-{hi}d"))
    bars = histories_yf([symbol], period="2y").get(symbol, [])
    _set_comparison_iv(ctx, hold)

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
            ctx.data["strike_grid_error"] = f"{type(exc).__name__}: {exc}"

    ctx.data["strikes_below_spot"] = sum(1 for k in ctx.strikes if k < ctx.spot)
    ctx.data["expiries_listed"] = len(expiries)
    ctx.data["chains_fetched"] = len(chosen)
    ctx.data["chain_source"] = "yfinance"
    if source == "auto" and symbol in _LIVE_ERRORS:
        ctx.data["live_unavailable"] = _LIVE_ERRORS[symbol]
    else:
        ctx.data.pop("live_unavailable", None)
    return ctx, bars



def _context(symbol: str, today: date, window: tuple[int, int],
             allowed_expiries: tuple[str, ...] | None = None):
    """Thread-safe equity-only yfinance context with repaired option IVs."""
    import yfinance as yf

    import core.yf_client as yfc
    from core.events import event_flags
    from core.iv_solve import repair_iv
    from core.pricing import q_for
    from core.regime import build_gates, compute_regime
    from core.surface import FRONT_DTE, iv_cm, pair_table, term_stats

    symbol = symbol.upper()
    fetch = yfc.PROXY.get(symbol, symbol)
    qdiv = q_for(symbol)
    tk = yf.Ticker(fetch)

    try:
        hist = tk.history(period="400d", auto_adjust=False)
    except Exception as exc:
        if _is_throttle_error(exc):
            raise EquityThrottleError(f"{symbol}: price-history rate limit") from exc
        raise
    if hist is None or len(hist) < 120:
        raise EquityDataError(f"{symbol}: insufficient yfinance price history")
    bars = [(idx.date() if hasattr(idx, "date") else idx,
             float(row["Open"]), float(row["High"]), float(row["Low"]),
             float(row["Close"])) for idx, row in hist.iterrows()]
    spot = bars[-1][4]

    curves: dict = {}
    provenance: dict = {}
    slices = []
    chain_failures: list[Exception] = []
    surface_verified = True
    try:
        options = list(tk.options or [])
    except Exception as exc:
        if _is_throttle_error(exc):
            raise EquityThrottleError(f"{symbol}: expiry-list rate limit") from exc
        raise
    if not options:
        raise EquityDataError(f"{symbol}: empty expiry response")

    allowed = set(allowed_expiries or ())
    for exp in options:
        if allowed and exp not in allowed:
            continue
        try:
            expiry = datetime.strptime(exp, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (expiry - today).days
        if not window[0] <= dte <= window[1]:
            continue
        try:
            chain = tk.option_chain(exp)
            if chain.calls is None or chain.puts is None or chain.calls.empty or chain.puts.empty:
                raise EquityDataError(f"{symbol} {exp}: empty option chain")
            t = max(dte, 1) / 365.0
            rc = repair_iv(chain.calls, spot, t, "C", q=qdiv)
            rp = repair_iv(chain.puts, spot, t, "P", q=qdiv)
            slc = yfc._slice_from_chain(expiry, dte, spot, rc, rp, qdiv)
            if slc is None:
                raise EquityDataError(f"{symbol} {exp}: no cross-checked ATM IV")
            verified_sources = {"bid/ask mid", "quoted IV cross-checked to bid/ask"}
            anchor_sources = []
            for frame in (rc, rp):
                match = frame[frame["strike"] == slc.atm_strike]
                if not match.empty:
                    anchor_sources.append(str(match.iloc[0].get("equityIvPriceSource")))
            if len(anchor_sources) < 2 or any(s not in verified_sources
                                               for s in anchor_sources):
                surface_verified = False
            curves[expiry.isoformat()] = {
                float(r["strike"]): float(r["impliedVolatility"])
                for _, r in rc.iterrows()
                if r.get("impliedVolatility") and float(r["impliedVolatility"]) > 0}
            provenance[expiry.isoformat()] = {
                float(r["strike"]): str(r.get("equityIvPriceSource") or "unpriced")
                for _, r in rc.iterrows()
                if r.get("impliedVolatility") and float(r["impliedVolatility"]) > 0}
            slices.append(slc)
        except Exception as exc:                    # noqa: BLE001
            if isinstance(exc, EquityThrottleError) or _is_throttle_error(exc):
                raise EquityThrottleError(f"{symbol} {exp}: option-chain rate limit") from exc
            chain_failures.append(exc)

    if not slices:
        detail = type(chain_failures[-1]).__name__ if chain_failures else "no expiry in window"
        raise EquityDataError(f"{symbol}: no usable cross-checked option chain ({detail})")
    slices.sort(key=lambda s: s.dte)

    iv30 = iv_cm(slices, 30) * 100
    ivh: list[float] = []
    ivp_src = "none - IV30/HAR proxy band"
    vix_sym = yfc.IVH_PROXY.get(symbol)
    if vix_sym:
        try:
            vh = yf.Ticker(vix_sym).history(period="1y")
            ivh = [float(c) for c in vh["Close"].tolist()
                   if yfc._num(c)]
            if len(ivh) >= 60:
                ivp_src = f"{vix_sym} history (index IV proxy)"
            else:
                ivh = []
        except Exception:
            ivh = []

    reg = compute_regime(bars, ivh, iv30)
    reg["spot"] = spot
    reg["ivp_src"] = ivp_src
    if not ivh:
        reg["ivp_proxy"] = True

    lo, hi = spot * 0.8, spot * 1.2
    strikes = sorted({k for s in slices
                      for k in (s.atm_strike, s.put25_strike, s.call25_strike)
                      if lo <= k <= hi})
    ev = event_flags(today, symbol, FRONT_DTE[1])
    last = bars[-1][0]
    gap = (today - last).days
    ctx = Context(symbol=symbol, spot=spot, today=today, slices=slices,
                  strikes=strikes, regime=reg, events=ev,
                  gates=build_gates(reg, ev, today), mode="yf", q=qdiv,
                  data={"session": str(last), "fresh": gap <= 4,
                        "gap_days": gap,
                        "note": f"yfinance delayed data; IVR src: {ivp_src}"})
    ctx.pairs = pair_table(slices, today)
    ctx.regime["term"] = term_stats(slices)
    ctx.data["iv_source"] = "per-row bid/ask cross-check with explicit fallback provenance"
    ctx.data["iv_curve"] = curves
    ctx.data["iv_curve_provenance"] = provenance
    ctx.data["option_chain_failures"] = len(chain_failures)
    ctx.data["volatility_inputs_verified"] = surface_verified
    return ctx
