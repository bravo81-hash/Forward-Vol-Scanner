"""Market/fundamental providers for the Value Entry Put Scanner."""
from __future__ import annotations

import hashlib
import math
import os
import random
import statistics
import time
from datetime import date, datetime, timedelta

from .engine import Company, OptionQuote, bs_put

MOCK_COMPANIES = {
    "AAPL": dict(name="Apple", sector="Technology", spot=228.0, market_cap=3.4e12,
                 normalized_eps=8.1, fcf_per_share=7.7, net_debt_ebitda=-0.2,
                 interest_coverage=28, profit_margin=.25, beta=1.15, rv=.24),
    "MSFT": dict(name="Microsoft", sector="Technology", spot=510.0, market_cap=3.8e12,
                 normalized_eps=16.9, fcf_per_share=14.8, net_debt_ebitda=-0.1,
                 interest_coverage=35, profit_margin=.36, beta=.98, rv=.22),
    "GOOGL": dict(name="Alphabet", sector="Communication Services", spot=194.0,
                  market_cap=2.4e12, normalized_eps=10.4, fcf_per_share=8.8,
                  net_debt_ebitda=-0.4, interest_coverage=50, profit_margin=.28,
                  beta=1.05, rv=.25),
    "AMZN": dict(name="Amazon", sector="Consumer Cyclical", spot=226.0,
                 market_cap=2.4e12, normalized_eps=7.2, fcf_per_share=5.4,
                 net_debt_ebitda=.5, interest_coverage=12, profit_margin=.11,
                 beta=1.25, rv=.29),
    "JPM": dict(name="JPMorgan Chase", sector="Financial Services", spot=291.0,
                market_cap=790e9, normalized_eps=21.0, fcf_per_share=22.0,
                book_value_per_share=128.0, net_debt_ebitda=0.0,
                interest_coverage=10, profit_margin=.31, beta=1.05, rv=.23),
    "BAC": dict(name="Bank of America", sector="Financial Services", spot=50.0,
                market_cap=380e9, normalized_eps=4.6, fcf_per_share=6.0,
                book_value_per_share=36.0, net_debt_ebitda=0.0,
                interest_coverage=10, profit_margin=.29, beta=1.25, rv=.38),
    "XOM": dict(name="Exxon Mobil", sector="Energy", spot=116.0, market_cap=500e9,
                normalized_eps=8.9, fcf_per_share=8.2, net_debt_ebitda=.2,
                interest_coverage=24, profit_margin=.11, beta=.88, rv=.24),
    "AAL": dict(name="American Airlines", sector="Industrials", spot=15.36,
                market_cap=10e9, normalized_eps=1.1, fcf_per_share=.35,
                net_debt_ebitda=5.8, interest_coverage=1.6, profit_margin=.025,
                beta=1.9, rv=.52),
}


def _friday_after(days: int) -> date:
    out = date.today() + timedelta(days=days)
    return out + timedelta(days=(4 - out.weekday()) % 7)


def mock_symbol(symbol: str) -> tuple[Company, list[OptionQuote]]:
    symbol = symbol.upper()
    raw = MOCK_COMPANIES.get(symbol)
    if raw is None:
        digest = int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16)
        spot = 40 + digest % 260
        raw = dict(name=f"{symbol} practice company", sector="Industrials", spot=spot,
                   market_cap=(5 + digest % 80) * 1e9, normalized_eps=spot / 19,
                   fcf_per_share=spot / 22, net_debt_ebitda=1.8,
                   interest_coverage=7, profit_margin=.12, beta=1.15, rv=.30)
    company = Company(
        symbol=symbol, name=raw["name"], sector=raw["sector"], spot=raw["spot"],
        market_cap=raw.get("market_cap"), normalized_eps=raw.get("normalized_eps"),
        fcf_per_share=raw.get("fcf_per_share"),
        book_value_per_share=raw.get("book_value_per_share"),
        net_debt_ebitda=raw.get("net_debt_ebitda"),
        interest_coverage=raw.get("interest_coverage"),
        profit_margin=raw.get("profit_margin"), beta=raw.get("beta"),
        realized_vol=raw.get("rv"), analyst_target=raw["spot"] * 1.06,
        earnings_date=(date.today() + timedelta(days=45)).isoformat(),
    )
    quotes: list[OptionQuote] = []
    base_iv = max(company.realized_vol * 1.18, .22)
    for dte in (75, 120, 180, 270, 360):
        expiry = _friday_after(dte)
        for pct in (.60, .65, .70, .75, .80, .85, .90, .95):
            strike = round(company.spot * pct / (1 if company.spot < 30 else 2.5)) * (
                1 if company.spot < 30 else 2.5)
            moneyness = max(1 - pct, 0)
            iv = base_iv + moneyness * .24 + (0.05 if symbol == "AAL" else 0)
            mid = max(bs_put(company.spot, strike, (expiry - date.today()).days, iv,
                             dividend_yield=company.dividend_yield), .03)
            spread = max(.04, mid * (.08 if mid >= .5 else .18))
            quotes.append(OptionQuote(
                expiry=expiry, strike=round(strike, 2),
                bid=round(max(mid - spread / 2, .01), 2),
                ask=round(mid + spread / 2, 2), iv=iv,
                open_interest=max(40, int(2400 * (1 - abs(.82 - pct)))),
                volume=max(2, int(180 * (1 - abs(.82 - pct)))),
                atm_iv=base_iv, buying_power=round(strike * 100 * .18, 2),
            ))
    return company, quotes


def mock_company_snapshot(symbol: str) -> tuple[Company, dict]:
    """Return deterministic discovery data without any external requests."""
    company, _ = mock_symbol(symbol)
    digest = int(hashlib.sha256(company.symbol.encode()).hexdigest()[:8], 16)
    high_52w = company.spot * (1.12 + (digest % 18) / 100)
    low_52w = company.spot * (0.62 + (digest % 14) / 100)
    if company.symbol == "AAL":
        high_52w, low_52w = 18.40, 10.09
    return company, {
        "high_52w": round(high_52w, 2),
        "low_52w": round(low_52w, 2),
        "average_dollar_volume": float(80_000_000 + digest % 900_000_000),
        "exchange": "PRACTICE",
    }


def _number(value, default=None):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return out if math.isfinite(out) else default


def _realized_vol(history) -> float | None:
    if history is None or len(history) < 65:
        return None
    closes = [float(value) for value in history["Close"].dropna().tolist()]
    returns = [math.log(b / a) for a, b in zip(closes[-61:-1], closes[-60:], strict=False)
               if a > 0 and b > 0]
    return statistics.stdev(returns) * math.sqrt(252) if len(returns) >= 30 else None


def _yahoo_company_snapshot(symbol: str):
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed") from exc
    ticker = yf.Ticker(symbol)
    history = ticker.history(period="1y", auto_adjust=False)
    if history is None or history.empty:
        raise RuntimeError(f"{symbol}: no price history")
    info = ticker.info or {}
    spot = _number(info.get("currentPrice") or info.get("regularMarketPrice"))
    if not spot:
        spot = float(history["Close"].dropna().iloc[-1])
    shares = _number(info.get("sharesOutstanding"))
    fcf = _number(info.get("freeCashflow"))
    total_debt = _number(info.get("totalDebt"))
    cash = _number(info.get("totalCash"), 0)
    ebitda = _number(info.get("ebitda"))
    leverage = ((total_debt - cash) / ebitda
                if total_debt is not None and ebitda and ebitda > 0 else None)
    warnings = []
    if not shares or fcf is None:
        warnings.append("FCF/share unavailable")
    if leverage is None:
        warnings.append("net-debt/EBITDA unavailable")
    company = Company(
        symbol=symbol, name=info.get("shortName") or symbol,
        sector=info.get("sector") or "Unknown", spot=spot,
        market_cap=_number(info.get("marketCap")),
        normalized_eps=_number(info.get("forwardEps") or info.get("trailingEps")),
        fcf_per_share=(fcf / shares if fcf is not None and shares else None),
        book_value_per_share=_number(info.get("bookValue")),
        net_debt_ebitda=leverage,
        interest_coverage=None,
        profit_margin=_number(info.get("profitMargins")),
        return_on_equity=_number(info.get("returnOnEquity")),
        beta=_number(info.get("beta")), dividend_yield=_number(info.get("dividendYield"), 0),
        analyst_target=_number(info.get("targetMeanPrice")),
        realized_vol=_realized_vol(history), earnings_date=None,
        data_warnings=warnings,
    )
    try:
        dates = ticker.get_earnings_dates(limit=4)
        future = [idx for idx in dates.index if idx.date() >= date.today()]
        company.earnings_date = min(future).date().isoformat() if future else None
    except Exception:
        company.data_warnings.append("earnings date unverified")
    volumes = history["Volume"].dropna() if "Volume" in history else []
    average_volume = float(volumes.tail(60).mean()) if len(volumes) else 0.0
    highs = history["High"].dropna() if "High" in history else []
    lows = history["Low"].dropna() if "Low" in history else []
    metadata = {
        "high_52w": float(highs.max()) if len(highs) else None,
        "low_52w": float(lows.min()) if len(lows) else None,
        "average_dollar_volume": average_volume * spot,
        "exchange": info.get("exchange") or info.get("fullExchangeName"),
    }
    return company, metadata, ticker


#: Yahoo rate-limits an 8-worker sweep, and the previous code turned a 429
#: into a dropped company. A partial universe then looked exactly like a
#: clean screen, so retry transient failures and let the caller classify
#: whatever is left.
FETCH_ATTEMPTS = int(os.getenv("FVS_YF_ATTEMPTS", "3"))
FETCH_BACKOFF_S = float(os.getenv("FVS_YF_BACKOFF_S", "1.5"))
_TRANSIENT_MARKERS = ("rate limit", "too many requests", "429", "timed out",
                      "timeout", "temporarily", "connection", "503", "502",
                      "remote end closed", "max retries")


def is_transient(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS)


def with_retry(fn, *args, attempts: int = FETCH_ATTEMPTS,
               backoff_s: float = FETCH_BACKOFF_S, **kwargs):
    """Call `fn`, retrying transient upstream failures with exponential backoff.

    Non-transient errors (an unknown ticker, a missing field) raise on the
    first attempt — retrying those just multiplies the wait.
    """
    last = None
    for attempt in range(1, max(attempts, 1) + 1):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last = exc
            if attempt >= attempts or not is_transient(exc):
                raise
            time.sleep(backoff_s * (2 ** (attempt - 1)) + random.uniform(0, 0.25))
    raise last  # pragma: no cover - loop always returns or raises


def yahoo_company_snapshot(symbol: str) -> tuple[Company, dict]:
    """Fetch only stock/fundamental data for universe discovery.

    Option expiries and chains are intentionally deferred until the shortlist
    reaches the Value Entry Put scan.
    """
    company, metadata, _ = _yahoo_company_snapshot(symbol)
    return company, metadata


def yahoo_symbol(symbol: str, min_dte: int, max_dte: int) -> tuple[Company, list[OptionQuote]]:
    company, _, ticker = _yahoo_company_snapshot(symbol)
    spot = company.spot

    expiries = []
    for raw in ticker.options or []:
        expiry = datetime.strptime(raw, "%Y-%m-%d").date()
        dte = (expiry - date.today()).days
        if min_dte <= dte <= max_dte:
            expiries.append(expiry)
    anchors = (60, 90, 135, 180, 270, 360)
    selected = sorted({min(expiries, key=lambda value: abs(
        (value - date.today()).days - anchor)) for anchor in anchors}) if expiries else []
    quotes: list[OptionQuote] = []
    for expiry in selected:
        chain = ticker.option_chain(expiry.isoformat())
        puts = chain.puts
        if puts is None or puts.empty:
            continue
        atm_rows = puts.iloc[(puts["strike"] - spot).abs().argsort()[:1]]
        atm_iv = _number(atm_rows.iloc[0].get("impliedVolatility")) if not atm_rows.empty else None
        for _, row in puts.iterrows():
            strike = _number(row.get("strike"))
            if not strike or not 0.55 * spot <= strike <= 0.97 * spot:
                continue
            bid, ask = _number(row.get("bid"), 0), _number(row.get("ask"), 0)
            iv = _number(row.get("impliedVolatility"))
            if not iv or ask <= 0 or bid < 0 or ask < bid:
                continue
            quotes.append(OptionQuote(
                expiry=expiry, strike=strike, bid=bid, ask=ask, iv=iv,
                open_interest=int(_number(row.get("openInterest"), 0)),
                volume=int(_number(row.get("volume"), 0)), atm_iv=atm_iv,
            ))
    return company, quotes
