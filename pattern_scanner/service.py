"""Forward-Vol-Scanner orchestration for the chart-pattern shortlist."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import date

import numpy as np
import pandas as pd

from core.events import trading_today
from core.stock_data import earnings_date_yf, histories_yf
from pattern_scanner.scanner import ACTIONABLE, SECTOR_ETFS, add_live_patterns, scan_patterns
from pattern_scanner.universe import universe_for
from selection.stock_radar import load_universe


def _frame(bars: list[dict]) -> pd.DataFrame:
    frame = pd.DataFrame(bars)
    if frame.empty:
        return frame
    if "date" in frame:
        frame.index = pd.to_datetime(frame.pop("date"))
    return frame[["open", "high", "low", "close", "volume"]].astype(float).dropna()


def _liquid(frame: pd.DataFrame) -> bool:
    if len(frame) < 120 or float(frame["close"].iloc[-1]) < 10:
        return False
    dollar_volume = (frame["close"] * frame["volume"]).tail(20).mean()
    return bool(dollar_volume >= 20_000_000)


def _mock_frame(close, today: date) -> list[dict]:
    close = np.asarray(close, dtype=float)
    wiggle = np.maximum(close * 0.006, 0.35)
    dates = pd.bdate_range(end=today, periods=len(close))
    return [
        {"date": stamp.date().isoformat(),
         "open": float(close[i - 1] if i else close[0]),
         "high": float(close[i] + wiggle[i]), "low": float(close[i] - wiggle[i]),
         "close": float(close[i]), "volume": 5_000_000.0}
        for i, stamp in enumerate(dates)
    ]


def _pattern_mock_histories(symbols: list[str], today: date) -> dict[str, list[dict]]:
    """Practice bundle containing deterministic examples of several geometries."""
    prior = np.linspace(70, 100, 100)
    base = np.r_[prior, np.tile([97.5, 99.8, 98.2, 99.7], 8), 100.8]
    cup_t = np.linspace(-1, 1, 80)
    cup = np.r_[np.linspace(60, 100, 70), 75 + 25 * cup_t * cup_t,
                np.linspace(99, 94, 8), np.linspace(94, 97, 7), 101]
    bottom = np.r_[np.linspace(120, 105, 80),
                   np.linspace(105, 90, 12), np.linspace(90, 104, 12),
                   np.linspace(104, 108, 8), np.linspace(108, 91, 14),
                   np.linspace(91, 107, 15), np.linspace(107, 107.5, 7), 110.5]
    ihs = np.r_[np.linspace(120, 100, 60), np.linspace(100, 90, 10),
                np.linspace(90, 102, 10), np.linspace(102, 82, 13),
                np.linspace(82, 103, 13), np.linspace(103, 91, 11),
                np.linspace(91, 104, 12), 106]
    triangle = []
    for low in (90, 92, 94, 96):
        triangle.extend(np.linspace(triangle[-1] if triangle else 92, 100, 6))
        triangle.extend(np.linspace(100, low, 6))
    triangle = np.r_[np.linspace(70, 92, 70), triangle, np.linspace(96, 101.5, 7)]
    hs = np.r_[np.linspace(80, 100, 60), np.linspace(100, 110, 10),
               np.linspace(110, 98, 10), np.linspace(98, 118, 13),
               np.linspace(118, 97, 13), np.linspace(97, 109, 11),
               np.linspace(109, 96, 12), 94]
    shapes = [base, cup, bottom, ihs, triangle, hs]
    histories = {}
    for i, symbol in enumerate(symbols):
        close = shapes[i % len(shapes)] if i < 18 else np.linspace(80, 105, 180)
        histories[symbol] = _mock_frame(close, today)
    histories["SPY"] = _mock_frame(np.linspace(400, 520, 180), today)
    return histories


def _earnings(rows: list[dict], today: date, source: str) -> tuple[list[dict], int]:
    if source == "mock":
        for row in rows:
            row.update(earnings_date=None, earnings_days=None, earnings_status="VERIFY")
        return rows, 0
    events: dict[str, date | None] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = {pool.submit(earnings_date_yf, row["ticker"]): row["ticker"] for row in rows}
        for job in as_completed(jobs):
            try:
                events[jobs[job]] = job.result()
            except Exception:
                events[jobs[job]] = None
    kept, excluded = [], 0
    for row in rows:
        event = events.get(row["ticker"])
        days = (event - today).days if event else None
        row.update(earnings_date=event.isoformat() if event else None,
                   earnings_days=days,
                   earnings_status=("CLEAR" if days is not None and days > 28 else
                                    "INSIDE_HOLD" if days is not None and days >= 0 else "VERIFY"))
        if row["earnings_status"] == "INSIDE_HOLD":
            excluded += 1
        else:
            kept.append(row)
    return kept, excluded


def run_pattern_scan(*, source: str = "yf", tickers: list[str] | None = None,
                     universe_limit: int | None = None, geometry_limit: int = 100,
                     context_limit: int = 20, final_limit: int = 10,
                     include_forming: bool = False, live: bool = False,
                     include_earnings: bool = True, today: date | None = None) -> dict:
    """Run bulk geometry -> context ranking -> final shortlist -> live overlay."""
    if source not in {"yf", "mock"}:
        raise ValueError("source must be yf or mock")
    today = today or trading_today()
    if tickers:
        symbols = list(dict.fromkeys(s.upper() for s in tickers if s.strip()))
    elif source == "mock":
        symbols = [row["symbol"] for row in load_universe()[:30]]
    else:
        symbols = universe_for("us")
    if universe_limit:
        symbols = symbols[:max(1, int(universe_limit))]
    universe_meta = {row["symbol"].upper(): row for row in load_universe()}
    sector_by_ticker = {symbol: str(meta.get("sector"))
                        for symbol, meta in universe_meta.items() if meta.get("sector")}
    fetch = list(dict.fromkeys([*symbols, "SPY", *SECTOR_ETFS]))
    if source == "mock":
        histories = _pattern_mock_histories(fetch, today)
    else:
        # Geometry is always built from completed sessions.  During the last
        # hour, TWS supplies a separate live quote overlay; Yahoo's unfinished
        # candle must never masquerade as close confirmation.
        histories = histories_yf(fetch, period="2y", completed_only=True)
    frames = {symbol: _frame(bars) for symbol, bars in histories.items() if bars}
    liquid = {symbol: frames[symbol] for symbol in symbols
              if symbol in frames and _liquid(frames[symbol])}
    if not liquid:
        raise RuntimeError("no liquid symbols with sufficient daily history were available")
    result = scan_patterns(
        liquid, bench_daily=frames.get("SPY"),
        sector_daily={symbol: frames[symbol] for symbol in SECTOR_ETFS if symbol in frames},
        sector_by_ticker=sector_by_ticker,
        geometry_limit=geometry_limit, context_limit=context_limit,
        final_limit=final_limit, include_forming=include_forming,
    )
    rows = result.rows
    excluded = 0
    if include_earnings and rows:
        rows, excluded = _earnings(rows, today, source)
    live_health = None
    live_excluded = 0
    if live and rows:
        rows, live_health, live_excluded = validate_pattern_rows(rows)
    rows.sort(key=lambda row: row["score"], reverse=True)
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
        row["tradingview_url"] = f"https://www.tradingview.com/chart/?symbol={row['ticker']}"
    return {
        "module": "price_action_patterns", "source": source,
        "session": today.isoformat(), "universe_requested": len(symbols),
        "data_as_of": max((frame.index[-1].date().isoformat() for frame in frames.values()),
                          default=None),
        "price_adjustment": "split-and-dividend adjusted",
        "symbols_with_bars": sum(symbol in frames for symbol in symbols),
        "liquid_symbols": len(liquid), "geometry_count": result.geometry_count,
        "context_count": result.context_count, "actionable_count": result.actionable_count,
        "earnings_excluded": excluded, "live_excluded": live_excluded,
        "live_health": live_health,
        "rows": rows,
        "portfolio_controls": {
            "timeframe": "completed daily bars only",
            "entry_timing": "near the daily close after confirmation",
            "risk_per_trade_pct": 1.0,
            "promotion_rule": "consider 1.5% only after 100 logged out-of-sample trades",
            "max_new_trades_per_day": 2,
            "max_open_scanner_trades": 5,
            "position_size_formula": "risk capital / absolute(entry - initial stop)",
        },
        "methodology_notes": [
            "Structural reversal patterns require a measurable preceding trend.",
            "Initial ATR stops and structural invalidations are displayed separately.",
            "The next tested support/resistance zone is preferred when it offers adequate room; measured moves remain fallbacks.",
            "Two-bar swing setups are daily-only, reject doji-like bars, target the 20 EMA and time out after five sessions.",
            "Rounding bottoms/tops are not mixed into this daily scanner; they require a separate weekly/monthly investor model.",
        ],
        "architecture": ["completed split-adjusted daily OHLCV", "pattern geometry",
                         "momentum + relative strength + volume + market/sector context",
                         "lifecycle and price-order sanity gates", "3-10 candidates",
                         "mandatory visual review", "separate TWS intraday validation"],
    }


def validate_pattern_rows(rows: list[dict]) -> tuple[list[dict], dict, int]:
    """Validate an existing shortlist without rerunning the Yahoo universe scan.

    The browser's live button is deliberately a cheap second-stage operation:
    it reuses the completed daily geometry already shown to the user and asks
    TWS only for the finalist quotes.
    """
    from core.ib_client import with_ib
    from core.stock_data import quotes_tws

    current = deepcopy(rows)
    quotes = with_ib(lambda ib: quotes_tws(ib, [row["ticker"] for row in current]))
    current, health = add_live_patterns(current, quotes)
    excluded = sum(row.get("live_status") not in ACTIONABLE for row in current)
    current = [row for row in current if row.get("live_status") in ACTIONABLE]
    current.sort(key=lambda row: row["score"], reverse=True)
    for rank, row in enumerate(current, 1):
        row["rank"] = rank
    return current, health, excluded
