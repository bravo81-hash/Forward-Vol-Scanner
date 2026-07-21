"""After-close history, company-event enrichment and last-hour stock quotes."""
from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from core.events import trading_today, upcoming_tier1
from selection.stock_radar import (ACTIVE_LIMIT, POOL_LIMIT, RESEARCH_LIMIT,
                                   POLICY_ID, analyse_symbol, apply_earnings,
                                   load_universe, rank_ideas)

NY = ZoneInfo("America/New_York")


def _finite(value) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _frame_bars(frame) -> list[dict]:
    out = []
    if frame is None or getattr(frame, "empty", True):
        return out
    for idx, row in frame.iterrows():
        close = _finite(row.get("Close"))
        if close is None or close <= 0:
            continue
        out.append({
            "date": (idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]),
            "open": _finite(row.get("Open")) or close,
            "high": _finite(row.get("High")) or close,
            "low": _finite(row.get("Low")) or close,
            "close": close, "volume": _finite(row.get("Volume")) or 0.0,
        })
    return out


def histories_yf(symbols: list[str], period: str = "2y", *,
                 completed_only: bool = True) -> dict[str, list[dict]]:
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed") from exc
    unique = list(dict.fromkeys([s.upper() for s in symbols]))
    data = yf.download(unique, period=period, interval="1d", auto_adjust=False,
                       group_by="ticker", progress=False, threads=True)
    result = {}
    multi = getattr(data.columns, "nlevels", 1) > 1
    for symbol in unique:
        try:
            frame = data[symbol] if multi else data
        except (KeyError, TypeError):
            continue
        bars = _frame_bars(frame)
        if completed_only and bars:
            now = datetime.now(NY)
            # Before the scheduled 16:10 ET close scan, Yahoo can expose an
            # incomplete current daily candle. Startup catch-up must always
            # use the previous completed session as its technical baseline.
            if (bars[-1]["date"] == now.date().isoformat()
                    and now.time().replace(tzinfo=None) < datetime.strptime("16:10", "%H:%M").time()):
                bars = bars[:-1]
        if bars:
            result[symbol] = bars
    return result


def _parse_event(value) -> date | None:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        for item in value:
            parsed = _parse_event(item)
            if parsed:
                return parsed
        return None
    if hasattr(value, "to_pydatetime"):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def earnings_date_yf(symbol: str) -> date | None:
    """Best-effort next earnings date. Unknown remains visibly unverified."""
    try:
        import yfinance as yf
        cal = yf.Ticker(symbol).calendar
        if cal is None:
            return None
        if isinstance(cal, dict):
            return _parse_event(cal.get("Earnings Date") or cal.get("EarningsDate"))
        if hasattr(cal, "index"):
            for label in ("Earnings Date", "EarningsDate"):
                if label in cal.index:
                    row = cal.loc[label]
                    values = row.tolist() if hasattr(row, "tolist") else row
                    return _parse_event(values)
    except Exception:  # noqa: BLE001 — event remains VERIFY, never silently clear
        return None
    return None


def _mock_bars(symbol: str, *, bearish: bool = False, base: float = 100.0,
               end: date | None = None) -> list[dict]:
    start = (end or date(2026, 7, 20)) - timedelta(days=269)
    bars, price = [], base
    seed = sum(ord(c) for c in symbol)
    for i in range(270):
        drift = (-0.0014 if bearish else 0.0015) + math.sin((i + seed) / 11) * 0.0008
        price = max(18, price * (1 + drift))
        if i >= 252:
            # Tight shelf close to the 20-day extreme so the trigger engine
            # produces a realistic ARMED breakout/breakdown in demo mode.
            price *= 1 + ((-0.00025 if bearish else 0.00025) * (i - 251))
        spread = price * (0.012 + (seed % 5) * 0.001)
        open_ = price * (1 + math.sin(i + seed) * 0.002)
        bars.append({"date": (start + timedelta(days=i)).isoformat(),
                     "open": open_, "high": max(open_, price) + spread / 2,
                     "low": min(open_, price) - spread / 2, "close": price,
                     "volume": 5_000_000 + (seed % 9) * 1_000_000})
    return bars


def histories_mock(universe: list[dict], today: date | None = None) -> dict[str, list[dict]]:
    out = {"SPY": _mock_bars("SPY", base=500, end=today)}
    for i, meta in enumerate(universe[:30]):
        out[meta["symbol"]] = _mock_bars(meta["symbol"], bearish=(i % 5 == 4),
                                          base=45 + (i % 12) * 18, end=today)
    return out


def scan_stocks(*, cadence: str = "daily", source: str = "yf",
                limit: int | None = None, previous_symbols: set[str] | None = None,
                today: date | None = None) -> dict:
    if cadence not in ("daily", "weekly"):
        raise ValueError("cadence must be daily or weekly")
    if source not in ("yf", "mock"):
        raise ValueError("after-close stock scans support yf or mock")
    today = today or trading_today()
    universe = load_universe()
    histories = (histories_mock(universe, today) if source == "mock" else
                 histories_yf([x["symbol"] for x in universe] + ["SPY"]))
    spy = histories.get("SPY")
    if not spy:
        raise RuntimeError("SPY benchmark history is unavailable")
    ideas = []
    for meta in universe:
        bars = histories.get(meta["symbol"])
        if not bars:
            continue
        idea = analyse_symbol(meta, bars, spy, today, cadence)
        if idea:
            idea["data_source"] = source
            ideas.append(idea)
    if not ideas:
        raise RuntimeError("no symbols passed the liquidity and setup filters")

    # Only event-check plausible finalists; scanning 70 individual optionable
    # names for calendars would be slow and unnecessary.
    pre = sorted(ideas, key=lambda x: x["score"], reverse=True)[:30]
    if source == "mock":
        events = {x["symbol"]: today + timedelta(days=45 + i * 3)
                  for i, x in enumerate(pre)}
        if pre:
            events[pre[0]["symbol"]] = today + timedelta(days=3)
    else:
        events = {}
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = {pool.submit(earnings_date_yf, x["symbol"]): x["symbol"] for x in pre}
            for job in as_completed(jobs):
                try:
                    events[jobs[job]] = job.result()
                except Exception:  # pragma: no cover — defensive boundary
                    events[jobs[job]] = None
    enriched = [apply_earnings(x, events.get(x["symbol"]), today) for x in pre]
    ranked_all = rank_ideas(enriched, cadence, previous_symbols,
                            RESEARCH_LIMIT, research=True)
    display_limit = min(max(int(limit or POOL_LIMIT), ACTIVE_LIMIT), POOL_LIMIT)
    ranked = []
    for i, idea in enumerate(ranked_all[:display_limit], 1):
        ranked.append({**idea, "rank": i,
                       "list_role": "ACTIVE" if i <= ACTIVE_LIMIT else "RESERVE"})
    research_pool = [{**idea, "shadow_only": int(idea["rank"]) > POOL_LIMIT}
                     for idea in ranked_all]
    return {
        "policy_id": POLICY_ID, "cadence": cadence, "source": source,
        "session": max((x["session"] for x in ranked), default=today.isoformat()),
        "created_at": datetime.now(NY).isoformat(), "universe_size": len(universe),
        "bars_available": len(histories) - 1, "setups_found": len(ideas),
        "active_limit": min(ACTIVE_LIMIT, len(ranked)), "pool_limit": len(ranked),
        "research_limit": len(research_pool), "limit": len(ranked),
        "candidates": ranked, "research_pool": research_pool,
        "upcoming_tier1": upcoming_tier1(today),
        "criteria": {
            "hard_filters": ["price >= $15", "20-day dollar volume >= $50m",
                             ">=220 daily bars", "ATR/price <=8%", "listed in liquid options universe"],
            "score_weights": ["trend and structure", "trigger proximity", "relative strength",
                              "liquidity", "momentum/volume", "defined-risk payoff", "event hygiene"],
            "diversification": "maximum two names per sector and per correlated cluster in the idea pool",
        },
    }


def quotes_yf(symbols: list[str]) -> dict[str, dict]:
    """Delayed fallback monitor; it never makes a card stageable."""
    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is not installed") from exc
    data = yf.download(symbols, period="1d", interval="1m", auto_adjust=False,
                       group_by="ticker", progress=False, threads=True)
    multi = getattr(data.columns, "nlevels", 1) > 1
    out = {}
    for symbol in symbols:
        try:
            frame = data[symbol] if multi else data
            closes = frame["Close"].dropna()
            if closes.empty:
                continue
            ts, px = closes.index[-1], float(closes.iloc[-1])
            out[symbol] = {"price": px, "captured_at": str(ts), "fresh": False,
                           "source": "yfinance delayed"}
        except Exception:  # noqa: BLE001
            continue
    return out


def quotes_tws(ib, symbols: list[str]) -> dict[str, dict]:
    from ib_insync import Stock
    from core.ib_client import quote_many
    contracts = [Stock(symbol, "SMART", "USD") for symbol in symbols]
    ib.qualifyContracts(*contracts)
    contracts = [c for c in contracts if c.conId]
    rows = quote_many(ib, contracts, want_greeks=False)
    captured = datetime.now(NY).isoformat()
    out = {}
    for contract in contracts:
        row = rows.get(contract.conId) or {}
        px = row.get("last") or row.get("mid") or row.get("close")
        if px:
            out[contract.symbol] = {"price": float(px), "captured_at": captured,
                                    "fresh": True, "source": "TWS live",
                                    "bid": row.get("bid"), "ask": row.get("ask"),
                                    "volume": row.get("volume")}
    return out
