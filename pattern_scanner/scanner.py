"""Two-pass chart-pattern scan and live validation.

Pass 1 is deliberately cheap: deterministic geometry over bulk adjusted OHLCV.
Pass 2 scores only the geometry shortlist using momentum, relative strength,
volume, the benchmark regime and an automatically inferred sector ETF.  TWS is
used only for the final shortlist, avoiding historical-data pacing bottlenecks.
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass

import numpy as np
import pandas as pd

from pattern_scanner import indicators as ind
from pattern_scanner.patterns import PatternCandidate, detect_all



SECTOR_ETFS = {
    "XLC": "Communication",
    "XLY": "Consumer discretionary",
    "XLP": "Consumer staples",
    "XLE": "Energy",
    "XLF": "Financials",
    "XLV": "Health care",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLRE": "Real estate",
    "XLK": "Technology",
    "XLU": "Utilities",
}

STATUS_BONUS = {
    "FORMING": 0.00,
    "NEAR_TRIGGER": 0.05,
    "TRIGGERED_INTRADAY": 0.08,
    "CLOSE_CONFIRMED": 0.09,
    "RETESTING": 0.10,
}
ACTIONABLE = {"NEAR_TRIGGER", "TRIGGERED_INTRADAY", "CLOSE_CONFIRMED", "RETESTING"}

# These are identities, not return-correlation guesses.  Unknown securities
# stay labelled Unknown even when a sector ETF is used as a neutral context
# proxy.  This prevents errors such as XBI being displayed as Industrials.
KNOWN_ETF_SECTORS = {
    **SECTOR_ETFS,
    "XBI": "Health care", "IBB": "Health care", "ARKG": "Health care",
    "SMH": "Technology", "SOXX": "Technology", "ARKK": "Multi-sector ETF",
    "XOP": "Energy", "OIH": "Energy", "KRE": "Financials",
    "XHB": "Consumer discretionary", "ITB": "Consumer discretionary",
    "GDX": "Materials", "GDXJ": "Materials", "XME": "Materials",
    "VNQ": "Real estate", "IYR": "Real estate", "TAN": "Energy",
    "SPY": "Broad market ETF", "QQQ": "Broad market ETF",
    "IWM": "Broad market ETF", "DIA": "Broad market ETF",
}

SECTOR_TO_ETF = {
    "Communication": "XLC", "Consumer discretionary": "XLY",
    "Consumer staples": "XLP", "Energy": "XLE", "Financials": "XLF",
    "Health care": "XLV", "Healthcare": "XLV", "Industrials": "XLI",
    "Defense": "XLI", "Materials": "XLB", "Real estate": "XLRE",
    "Technology": "XLK", "Semiconductors": "XLK", "Utilities": "XLU",
}


@dataclass
class PipelineResult:
    rows: list[dict]
    geometry_count: int
    context_count: int
    actionable_count: int
    symbols_scanned: int


def _clip(x):
    return float(max(0.0, min(1.0, x)))


def _returns(d, n):
    if d is None or len(d) <= n:
        return None
    c = d["close"]
    return float(c.iloc[-1] / c.iloc[-1 - n] - 1)


def _percentiles(values: dict[str, float]) -> dict[str, int]:
    xs = sorted(values.values())
    out = {}
    for key, value in values.items():
        if len(xs) < 2:
            out[key] = 50
        else:
            out[key] = int(round(100 * bisect.bisect_left(xs, value) / (len(xs) - 1)))
    return out


def _momentum(d: pd.DataFrame, side: str):
    c = d["close"]
    e20, e50 = ind.ema(c, 20), ind.ema(c, 50)
    rsi = float(ind.rsi(c, 14).iloc[-1])
    ret21 = _returns(d, 21) or 0.0
    if side == "long":
        trend = (int(c.iloc[-1] > e20.iloc[-1]) + int(e20.iloc[-1] > e50.iloc[-1])) / 2
        rsi_score = _clip(1 - abs(rsi - 62) / 28)
        return _clip(0.55 * trend + 0.25 * rsi_score + 0.20 * _clip((ret21 + 0.03) / 0.15)), rsi, ret21
    trend = (int(c.iloc[-1] < e20.iloc[-1]) + int(e20.iloc[-1] < e50.iloc[-1])) / 2
    rsi_score = _clip(1 - abs(rsi - 38) / 28)
    return _clip(0.55 * trend + 0.25 * rsi_score + 0.20 * _clip((-ret21 + 0.03) / 0.15)), rsi, ret21


def _volume_score(d: pd.DataFrame, status: str):
    v = d["volume"].astype(float)
    avg = float(v.iloc[-21:-1].mean()) if len(v) > 21 else float(v.mean())
    ratio = float(v.iloc[-1] / avg) if avg > 0 else 0.0
    if status in ("CLOSE_CONFIRMED", "TRIGGERED_INTRADAY"):
        score = _clip((ratio - 0.8) / 0.8)  # 1.2x -> 0.5; 1.6x -> 1
    else:
        # Before a break, quiet trade is healthy; neither reward nor reject an
        # unfinished current daily volume bar aggressively.
        score = _clip(1 - abs(ratio - 0.75) / 0.75)
    return score, ratio


def _aligned_score(side: str, bias: str | None):
    if bias in (None, "neutral"):
        return 0.5
    return 1.0 if ((side == "long") == (bias == "bullish")) else 0.0


def _sector_for(ticker: str, daily: pd.DataFrame, sectors: dict[str, pd.DataFrame],
                bench_ret: float | None, known_sector: str | None = None):
    """Return an identity-backed sector label and a context proxy.

    Correlation may choose a proxy for scoring, but never changes the displayed
    economic identity of an unknown ticker.
    """
    identity = KNOWN_ETF_SECTORS.get(ticker) or known_sector
    normalized = {"Healthcare": "Health care", "Real Estate": "Real estate",
                  "Consumer Discretionary": "Consumer discretionary",
                  "Consumer Staples": "Consumer staples"}.get(identity, identity)
    known_etf = SECTOR_TO_ETF.get(normalized or "")
    if ticker in sectors:
        chosen, corr = ticker, 1.0
    elif known_etf in sectors:
        chosen, corr = known_etf, 1.0
    else:
        tr = daily["close"].pct_change().tail(63)
        chosen, corr = None, -1.0
        for etf, sd in sectors.items():
            sr = sd["close"].pct_change().tail(63)
            z = pd.concat([tr, sr], axis=1).dropna()
            value = float(z.iloc[:, 0].corr(z.iloc[:, 1])) if len(z) >= 30 else np.nan
            if np.isfinite(value) and value > corr:
                chosen, corr = etf, value
    if chosen is None or corr < 0.35:
        return normalized or "Unknown", None, corr, 0.5, "identity" if normalized else "unknown"
    sret = _returns(sectors[chosen], 63)
    rel = (sret - bench_ret) if sret is not None and bench_ret is not None else sret
    return normalized or "Unknown", rel, corr, None, "identity" if normalized else "correlation_proxy"


def _direction_read(d: pd.DataFrame) -> str:
    """Simple broad-market direction used only as a context component."""
    close = d["close"].astype(float)
    e20, e50 = ind.ema(close, 20), ind.ema(close, 50)
    last = float(close.iloc[-1])
    if last > float(e20.iloc[-1]) > float(e50.iloc[-1]):
        return "bullish"
    if last < float(e20.iloc[-1]) < float(e50.iloc[-1]):
        return "bearish"
    return "neutral"


def scan_patterns(bundle: dict, bench_daily: pd.DataFrame | None = None,
                  sector_daily: dict[str, pd.DataFrame] | None = None,
                  sector_by_ticker: dict[str, str] | None = None,
                  geometry_limit: int = 100, context_limit: int = 20,
                  final_limit: int = 10, include_forming: bool = False,
                  min_geometry: float = 0.42, min_context: float = 0.40) -> PipelineResult:
    """Run geometry -> context -> actionable shortlist.

    ``bundle`` accepts either ``{ticker: daily}`` or the main scanner's
    ``{ticker: (daily, weekly)}`` shape.
    """
    daily_map = {t: (x[0] if isinstance(x, tuple) else x) for t, x in bundle.items()}
    geometry = []
    for ticker, daily in daily_map.items():
        for candidate in detect_all(ticker, daily):
            if candidate.geometry_score >= min_geometry:
                geometry.append(candidate)
    geometry.sort(key=lambda c: (c.status in ACTIONABLE, c.geometry_score), reverse=True)
    geometry = geometry[:max(1, geometry_limit)]

    bench_ret = _returns(bench_daily, 63)
    bench_bias = None
    if bench_daily is not None and len(bench_daily) >= 60:
        try:
            bench_bias = _direction_read(bench_daily)
        except Exception:
            bench_bias = None

    rs_raw = {}
    for ticker, daily in daily_map.items():
        value = _returns(daily, 63)
        if value is not None:
            rs_raw[ticker] = value - bench_ret if bench_ret is not None else value
    rs_pct = _percentiles(rs_raw)
    sectors = sector_daily or {}

    rows = []
    for c in geometry:
        daily = daily_map[c.ticker]
        momentum, rsi14, ret21 = _momentum(daily, c.side)
        rp = rs_pct.get(c.ticker, 50)
        rs_score = rp / 100 if c.side == "long" else (100 - rp) / 100
        volume, volx = _volume_score(daily, c.status)
        sector, sector_rel, sector_corr, neutral_sector, sector_source = _sector_for(
            c.ticker, daily, sectors, bench_ret, (sector_by_ticker or {}).get(c.ticker))
        if neutral_sector is not None:
            sector_score = neutral_sector
        else:
            sector_score = _clip(0.5 + (sector_rel or 0.0) / 0.20)
            if c.side == "short":
                sector_score = 1 - sector_score
        market_score = _aligned_score(c.side, bench_bias)
        context = (0.32 * momentum + 0.25 * rs_score + 0.18 * sector_score
                   + 0.15 * volume + 0.10 * market_score)
        final = _clip(0.55 * c.geometry_score + 0.35 * context
                      + STATUS_BONUS.get(c.status, 0.0))
        if context < min_context and c.geometry_score < 0.75:
            continue
        row = c.row()
        row.update({
            "score": round(final, 3),
            "context_score": round(context, 3),
            "momentum_score": round(momentum, 3),
            "rsi14": round(rsi14, 1),
            "ret21": round(ret21 * 100, 1),
            "rs": round(rs_raw.get(c.ticker, 0.0) * 100, 1) if c.ticker in rs_raw else None,
            "rs_pct": rp,
            "volume_score": round(volume, 3),
            "volx": round(volx, 2),
            "market_bias": bench_bias,
            "market_aligned": market_score == 1.0,
            "sector": sector,
            "sector_source": sector_source,
            "sector_rel": round(sector_rel * 100, 1) if sector_rel is not None else None,
            "sector_corr": round(sector_corr, 2) if np.isfinite(sector_corr) else None,
            "last": round(float(daily["close"].iloc[-1]), 2),
            "data_as_of": pd.Timestamp(daily.index[-1]).date().isoformat(),
            "atr": round(float(ind.atr(daily).iloc[-1]), 2),
            "spark": [round(float(x), 4) for x in daily["close"].tail(90)],
            "chart": {
                "dates": [pd.Timestamp(x).date().isoformat() for x in daily.tail(220).index],
                "open": [round(float(x), 4) for x in daily["open"].tail(220)],
                "high": [round(float(x), 4) for x in daily["high"].tail(220)],
                "low": [round(float(x), 4) for x in daily["low"].tail(220)],
                "close": [round(float(x), 4) for x in daily["close"].tail(220)],
            },
            "review": "REQUIRED",
            "review_reason": "Confirm clean shape, trade location and nearby support/resistance on chart",
            "time_exit": 20,
        })
        rows.append(row)

    rows.sort(key=lambda r: (r["status"] in ACTIONABLE, r["score"]), reverse=True)
    context_rows = rows[:max(1, context_limit)]
    final_rows = [r for r in context_rows if include_forming or r["status"] in ACTIONABLE]
    final_rows = final_rows[:max(1, final_limit)]
    return PipelineResult(final_rows, len(geometry), len(context_rows),
                          sum(r["status"] in ACTIONABLE for r in context_rows),
                          len(daily_map))


def live_pattern_status(row: dict, live_price: float):
    atr = float(row.get("atr") or 0.0)
    trigger, invalid = float(row["trigger"]), float(row["invalidation"])
    target = float(row.get("target", float("inf") if row.get("side") == "long"
                           else float("-inf")))
    side = row["side"]
    if atr <= 0:
        return row.get("status", ""), None
    pen, ret = 0.25 * atr, 0.35 * atr
    if side == "long":
        distance = (trigger - live_price) / atr
        if live_price >= target or live_price > trigger + 2.0 * atr:
            status = "EXPIRED"
        elif live_price < invalid:
            status = "FAILED"
        elif row.get("breakout_age") not in (None, 0) and trigger - ret <= live_price <= trigger + ret:
            status = "RETESTING"
        elif live_price > trigger + pen:
            status = ("CLOSE_CONFIRMED" if row.get("status") == "CLOSE_CONFIRMED"
                      else "TRIGGERED_INTRADAY")
        elif abs(trigger - live_price) <= atr:
            status = "NEAR_TRIGGER"
        else:
            status = row.get("status", "FORMING")
    else:
        distance = (live_price - trigger) / atr
        if live_price <= target or live_price < trigger - 2.0 * atr:
            status = "EXPIRED"
        elif live_price > invalid:
            status = "FAILED"
        elif row.get("breakout_age") not in (None, 0) and trigger - ret <= live_price <= trigger + ret:
            status = "RETESTING"
        elif live_price < trigger - pen:
            status = ("CLOSE_CONFIRMED" if row.get("status") == "CLOSE_CONFIRMED"
                      else "TRIGGERED_INTRADAY")
        elif abs(live_price - trigger) <= atr:
            status = "NEAR_TRIGGER"
        else:
            status = row.get("status", "FORMING")
    return status, round(distance, 2)


def add_live_patterns(rows: list[dict], quotes: dict[str, dict]):
    """Overlay final-shortlist quotes supplied by Forward-Vol-Scanner.

    Historical geometry never crawls through TWS. The caller obtains one
    snapshot per finalist through the application's shared quote layer.
    """
    fresh = 0
    for row in rows:
        quote = quotes.get(row["ticker"]) or {}
        price = quote.get("price")
        if price is None:
            continue
        row["live"] = round(float(price), 2)
        row["live_status"], row["live_distance_atr"] = live_pattern_status(
            row, float(price))
        row["live_source"] = quote.get("source")
        fresh += int(bool(quote.get("fresh")))
    total = len(rows)
    return rows, {"fresh": fresh, "total": total,
                  "ok": total == 0 or fresh / total >= 0.6}
