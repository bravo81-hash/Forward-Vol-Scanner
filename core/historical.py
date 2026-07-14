"""Free, cached historical regime snapshot for the OptionNet test workflow.

No historical option chain is claimed.  Daily underlying and volatility-index
history derives trend, realised volatility, IV percentile and forward VRP.
VIX9D/VIX3M and SKEW are explicitly labelled proxies for term and skew.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from functools import lru_cache

from core.events import opex_day, trading_today
from core.regime import compute_regime

PRICE_TICKER = {"SPX": "^GSPC", "SPY": "SPY", "NDX": "^NDX", "QQQ": "QQQ",
                "RUT": "^RUT", "IWM": "IWM"}
IV_TICKER = {"SPX": "^VIX", "SPY": "^VIX", "NDX": "^VXN", "QQQ": "^VXN",
             "RUT": "^RVX", "IWM": "^RVX"}
PROXY_TICKERS = ("^VIX9D", "^VIX3M", "^SKEW")


@lru_cache(maxsize=96)
def _yf_history(ticker: str, year: int) -> tuple[tuple, ...]:
    try:
        import yfinance as yf
    except ImportError as exc:  # pragma: no cover - installation problem
        raise RuntimeError("automatic history requires yfinance; install requirements.txt") from exc
    start = date(year - 2, 1, 1)
    end = min(date(year + 1, 1, 5), trading_today() + timedelta(days=2))
    df = yf.Ticker(ticker).history(start=start.isoformat(), end=end.isoformat(),
                                   auto_adjust=False)
    if df is None or df.empty:
        return ()
    rows = []
    for idx, row in df.iterrows():
        d = idx.date() if hasattr(idx, "date") else idx
        try:
            values = (d, float(row["Open"]), float(row["High"]),
                      float(row["Low"]), float(row["Close"]))
        except (KeyError, TypeError, ValueError):
            continue
        if values[4] > 0:
            rows.append(values)
    return tuple(rows)


def _last(rows: list[tuple]) -> float | None:
    return float(rows[-1][4]) if rows else None


def free_daily_inputs(symbol: str, as_of: date, *, include_as_of: bool = False,
                      loader=None) -> dict:
    """Underlying bars and IV-index closes for a live TWS data fallback."""
    symbol = symbol.upper()
    if symbol not in PRICE_TICKER:
        raise ValueError(f"free daily history is unavailable for {symbol}")
    loader = loader or (lambda ticker, day: list(_yf_history(ticker, day.year)))
    try:
        tickers = (PRICE_TICKER[symbol], IV_TICKER[symbol])
        with ThreadPoolExecutor(max_workers=2) as pool:
            price_rows, iv_rows = pool.map(lambda ticker: list(loader(ticker, as_of)), tickers)
    except Exception as exc:
        raise RuntimeError(f"free daily-history lookup failed: {exc}") from exc
    accept = ((lambda d: d <= as_of) if include_as_of else (lambda d: d < as_of))
    bars = [row for row in price_rows if accept(row[0])][-320:]
    ivh = [float(row[4]) for row in iv_rows if accept(row[0])][-252:]
    if len(bars) < 150:
        raise RuntimeError(f"free history returned only {len(bars)} price bars for {symbol}")
    return {"bars": bars, "ivh": ivh,
            "price_source": f"yfinance {PRICE_TICKER[symbol]}",
            "iv_source": f"yfinance {IV_TICKER[symbol]}"}


def auto_historical_snapshot(symbol: str, as_of: date, loader=None) -> dict:
    symbol = symbol.upper()
    if symbol not in PRICE_TICKER:
        raise ValueError(f"automatic history is unavailable for {symbol}")
    loader = loader or (lambda ticker, day: list(_yf_history(ticker, day.year)))
    tickers = [PRICE_TICKER[symbol], IV_TICKER[symbol], *PROXY_TICKERS]

    def safe_load(ticker: str) -> list[tuple]:
        try:
            return list(loader(ticker, as_of))
        except Exception:  # optional proxy failure must not break required history
            return []

    with ThreadPoolExecutor(max_workers=len(tickers)) as pool:
        fetched = dict(zip(tickers, pool.map(safe_load, tickers)))
    # The test entry is 15:30 ET. Daily closes stamped with ``as_of`` are not
    # known yet, so auto mode deliberately stops at the prior trading close.
    rows = {ticker: [r for r in data if r[0] < as_of]
            for ticker, data in fetched.items()}
    bars = rows[PRICE_TICKER[symbol]]
    iv_rows = rows[IV_TICKER[symbol]]
    if len(bars) < 120:
        raise RuntimeError(f"not enough historical price data for {symbol} on {as_of}")
    if len(iv_rows) < 60:
        raise RuntimeError(f"not enough {IV_TICKER[symbol]} history for {as_of}")

    bars = bars[-320:]
    iv_hist = [float(r[4]) for r in iv_rows[-252:]]
    iv30 = iv_hist[-1]
    reg = compute_regime(bars, iv_hist, iv30)

    v9, v3m, skew = (_last(rows[t]) for t in PROXY_TICKERS)
    if v9 and v3m:
        rat_front, rat_back = v9 / iv30, iv30 / v3m
        term = ("INVERTED FRONT" if rat_front > 1.0 else
                "STEEP CONTANGO" if rat_back < .93 else
                "CONTANGO" if rat_back < .99 else "FLAT")
        term_quality = "SPX volatility-index proxy" if symbol in ("SPX", "SPY") else "broad SPX proxy"
    else:
        term, term_quality = "FLAT", "unavailable; conservative flat default"
    skew_rich = bool(skew and skew >= 130)
    rr_proxy = round(iv30 * .35, 2) if skew_rich else 0.0
    intent = "bull" if reg["bias"] > 0 else "bear" if reg["bias"] < 0 else "neutral"
    ox = opex_day(as_of.year, as_of.month)
    event = "OPEX" if ox.day - 4 <= as_of.day <= ox.day else "NONE"

    available = sum(bool(x) for x in (v9, v3m, skew))
    confidence = "HIGH" if symbol in ("SPX", "SPY") and available == 3 else "MEDIUM" if available >= 2 else "LOW"
    cutoff = bars[-1][0]
    warnings = [f"Signals stop at the prior close ({cutoff}); ONE supplies the actual 15:30 legs and fill."]
    warnings.append("Term and skew are free index proxies, not the historical 25Δ option surface.")
    warnings.append("Only OpEx week is detected automatically; override Advanced if ONE shows a known macro event.")
    if symbol not in ("SPX", "SPY"):
        warnings.append("VIX9D/VIX3M/SKEW are SPX-market proxies for this underlying.")
    if not skew:
        warnings.append("Historical SKEW was unavailable; ordinary skew is assumed unless overridden.")
    return {
        "symbol": symbol, "entry_date": as_of.isoformat(), "data_cutoff": cutoff.isoformat(),
        "spot": round(bars[-1][4], 2),
        "iv30": round(iv30, 2), "rv21": reg["rv21"], "vrp_fwd": reg["vrp_fwd"],
        "iv_band": reg["vol_state"], "iv_pctl": reg["iv_pctl"],
        "rr25": rr_proxy, "skew_state": "RICH PUT PROXY" if skew_rich else "ORDINARY PROXY",
        "term": term, "trend": reg["trend"], "bias": reg["bias"], "intent": intent,
        "event": event, "confidence": confidence, "warnings": warnings,
        "sources": {"price": PRICE_TICKER[symbol], "iv": IV_TICKER[symbol],
                    "term": term_quality, "skew": f"^SKEW {skew:.2f}" if skew else "unavailable"},
    }
