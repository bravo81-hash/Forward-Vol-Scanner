"""Pure ranking and trigger logic for the single-stock Opportunity Radar.

The full liquid universe is scanned after the close.  Only the saved top five
(daily) or top ten (Friday/weekly) are touched by TWS during the last hour, so
the live desk remains comfortably inside IBKR market-data and pacing limits.
"""
from __future__ import annotations

import math
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import quote

import yaml

from core.models import Leg
from core.pricing import risk_profile, struct_greeks, struct_value
from execution.optionstrat import optionstrat_url

POLICY_ID = "stock-radar-v1"
DAILY_LIMIT = 5
WEEKLY_LIMIT = 10
MIN_BARS = 220
MIN_PRICE = 15.0
MIN_DOLLAR_VOLUME = 50_000_000.0
MAX_ATR_PCT = 8.0
MAX_PER_SECTOR = 2
MAX_PER_CLUSTER = 2


def load_universe(path: str | Path | None = None) -> list[dict]:
    path = Path(path or Path(__file__).parents[1] / "config" / "stock_universe.yaml")
    with path.open(encoding="utf-8") as fh:
        rows = yaml.safe_load(fh).get("stocks", [])
    return [{**row, "symbol": str(row["symbol"]).upper()} for row in rows]


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _ema(xs: list[float], period: int) -> float:
    if not xs:
        return 0.0
    alpha, out = 2.0 / (period + 1), xs[0]
    for x in xs[1:]:
        out = alpha * x + (1 - alpha) * out
    return out


def _rsi(xs: list[float], period: int = 14) -> float:
    if len(xs) <= period:
        return 50.0
    changes = [xs[i] - xs[i - 1] for i in range(len(xs) - period, len(xs))]
    gains = _mean([max(x, 0.0) for x in changes])
    losses = _mean([max(-x, 0.0) for x in changes])
    if losses == 0:
        return 100.0
    rs = gains / losses
    return 100 - 100 / (1 + rs)


def _atr(bars: list[dict], period: int = 14) -> float:
    rows = bars[-(period + 1):]
    if len(rows) < 2:
        return 0.0
    tr = []
    for prev, row in zip(rows, rows[1:]):
        tr.append(max(row["high"] - row["low"],
                      abs(row["high"] - prev["close"]),
                      abs(row["low"] - prev["close"])))
    return _mean(tr)


def _adx(bars: list[dict], period: int = 14) -> float:
    rows = bars[-(period + 1):]
    if len(rows) < period + 1:
        return 0.0
    plus, minus, trs = [], [], []
    for prev, row in zip(rows, rows[1:]):
        up, down = row["high"] - prev["high"], prev["low"] - row["low"]
        plus.append(up if up > down and up > 0 else 0.0)
        minus.append(down if down > up and down > 0 else 0.0)
        trs.append(max(row["high"] - row["low"],
                       abs(row["high"] - prev["close"]),
                       abs(row["low"] - prev["close"])))
    tr = sum(trs)
    if tr <= 0:
        return 0.0
    pdi, mdi = 100 * sum(plus) / tr, 100 * sum(minus) / tr
    return 100 * abs(pdi - mdi) / max(pdi + mdi, 1e-9)


def _ret(xs: list[float], days: int) -> float:
    if len(xs) <= days or xs[-days - 1] <= 0:
        return 0.0
    return xs[-1] / xs[-days - 1] - 1


def _round_strike(value: float) -> float:
    if value < 50:
        step = 1.0
    elif value < 250:
        step = 2.5
    elif value < 500:
        step = 5.0
    else:
        step = 10.0
    return round(value / step) * step


def _third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_friday = first + timedelta(days=(4 - first.weekday()) % 7)
    return first_friday + timedelta(days=14)


def monthly_expiry_near(today: date, target_dte: int = 60) -> date:
    target = today + timedelta(days=target_dte)
    candidates = []
    y, m = today.year, today.month
    for add in range(1, 6):
        mm = m + add
        yy = y + (mm - 1) // 12
        mm = (mm - 1) % 12 + 1
        exp = _third_friday(yy, mm)
        if 40 <= (exp - today).days <= 85:
            candidates.append(exp)
    return min(candidates, key=lambda d: abs((d - target).days))


def tradingview_url(symbol: str, exchange: str) -> str:
    return "https://www.tradingview.com/chart/?symbol=" + quote(f"{exchange}:{symbol}")


def _strategy(direction: str, price: float, target: float, today: date,
              iv: float) -> dict:
    expiry = monthly_expiry_near(today)
    cp = "C" if direction == "BULL" else "P"
    long_k = _round_strike(price)
    short_k = _round_strike(target)
    if direction == "BULL" and short_k <= long_k:
        short_k = _round_strike(long_k * 1.08)
    if direction == "BEAR" and short_k >= long_k:
        short_k = _round_strike(long_k * 0.92)
    legs = [
        {"cp": cp, "strike": long_k, "expiry": expiry.isoformat(), "qty": 1, "iv": iv},
        {"cp": cp, "strike": short_k, "expiry": expiry.isoformat(), "qty": -1, "iv": iv},
    ]
    width = abs(short_k - long_k)
    label = f"{long_k:g}/{short_k:g} {'call' if cp == 'C' else 'put'} debit spread"
    return {
        "key": "call_debit" if cp == "C" else "put_debit",
        "label": label, "expiry": expiry.isoformat(), "legs_raw": legs,
        "width": width, "max_debit": round(width * 0.45, 2),
        "rule": "Pay no more than 45% of width; use the live combination midpoint.",
    }


def analyse_symbol(meta: dict, bars: list[dict], spy_bars: list[dict],
                   today: date, cadence: str = "daily") -> dict | None:
    if len(bars) < MIN_BARS or len(spy_bars) < MIN_BARS:
        return None
    closes = [float(x["close"]) for x in bars]
    highs = [float(x["high"]) for x in bars]
    lows = [float(x["low"]) for x in bars]
    volumes = [float(x.get("volume") or 0) for x in bars]
    spy = [float(x["close"]) for x in spy_bars]
    close, last = closes[-1], bars[-1]
    if close < MIN_PRICE:
        return None
    adv20 = _mean(volumes[-20:])
    dollar_vol = adv20 * close
    if dollar_vol < MIN_DOLLAR_VOLUME:
        return None

    sma20, sma50, sma200 = _mean(closes[-20:]), _mean(closes[-50:]), _mean(closes[-200:])
    ema21 = _ema(closes[-80:], 21)
    atr = _atr(bars)
    atr_pct = 100 * atr / close if close else 0
    if atr <= 0 or atr_pct > MAX_ATR_PCT:
        return None
    rsi, adx = _rsi(closes), _adx(bars)
    macd_hist = (_ema(closes[-80:], 12) - _ema(closes[-80:], 26)) - _ema(
        [_ema(closes[:i], 12) - _ema(closes[:i], 26)
         for i in range(max(27, len(closes) - 45), len(closes) + 1)], 9)
    prior20h, prior20l = max(highs[-21:-1]), min(lows[-21:-1])
    recent5h, recent5l = max(highs[-5:]), min(lows[-5:])
    rs20 = _ret(closes, 20) - _ret(spy, 20)
    rs60 = _ret(closes, 60) - _ret(spy, 60)
    volume_ratio = volumes[-1] / adv20 if adv20 else 0
    gap_pct = 100 * (last["open"] / bars[-2]["close"] - 1) if bars[-2]["close"] else 0

    bull_trend = close > sma50 > sma200 and ema21 >= sma50
    bear_trend = close < sma50 < sma200 and ema21 <= sma50
    near_high = (prior20h - close) <= 0.75 * atr
    near_low = (close - prior20l) <= 0.75 * atr
    pullback = bull_trend and abs(close - ema21) <= 0.9 * atr and close >= sma50
    failed_rally = bear_trend and abs(close - ema21) <= 0.9 * atr and close <= sma50

    if bull_trend and near_high:
        direction, setup = "BULL", "breakout"
        trigger = prior20h * 1.001
        invalidation = min(recent5l, ema21 - 0.35 * atr)
    elif pullback:
        direction, setup = "BULL", "pullback_reclaim"
        trigger = last["high"] * 1.001
        invalidation = recent5l - 0.2 * atr
    elif bear_trend and near_low:
        direction, setup = "BEAR", "breakdown"
        trigger = prior20l * 0.999
        invalidation = max(recent5h, ema21 + 0.35 * atr)
    elif failed_rally:
        direction, setup = "BEAR", "failed_rally"
        trigger = last["low"] * 0.999
        invalidation = recent5h + 0.2 * atr
    else:
        return None

    risk = abs(trigger - invalidation)
    if risk < 0.35 * atr or risk > 3.0 * atr:
        return None
    target = trigger + 2 * risk if direction == "BULL" else trigger - 2 * risk
    rr = abs(target - trigger) / risk
    distance_atr = abs(trigger - close) / atr
    extended_atr = abs(close - ema21) / atr
    if extended_atr > 2.5:
        return None

    liquidity = min(15.0, max(0.0, 5 + 5 * math.log10(dollar_vol / MIN_DOLLAR_VOLUME)))
    trend_score = 15 + min(adx, 35) / 3.5
    trigger_score = max(0.0, 20 - 10 * distance_atr)
    aligned_rs = rs20 if direction == "BULL" else -rs20
    aligned_rs60 = rs60 if direction == "BULL" else -rs60
    rs_score = max(0.0, min(15.0, 7.5 + 40 * aligned_rs + 20 * aligned_rs60))
    momentum_ok = (macd_hist > 0 and direction == "BULL") or (macd_hist < 0 and direction == "BEAR")
    momentum = (5.0 if momentum_ok else 1.0) + min(volume_ratio, 2.0) * 2.5
    rr_score = min(10.0, rr * 5)
    candle_ok = ((last["close"] >= last["open"] and direction == "BULL") or
                 (last["close"] <= last["open"] and direction == "BEAR"))
    readiness = 5.0 if candle_ok else 2.0
    score = liquidity + trend_score + trigger_score + rs_score + momentum + rr_score + readiness
    if cadence == "weekly":
        score += max(-5, min(5, aligned_rs60 * 35))
    post_event = abs(gap_pct) >= 6 and volume_ratio >= 1.5
    iv_est = max(0.15, min(1.2, atr_pct / 100 * math.sqrt(252)))
    strategy = _strategy(direction, trigger, target, today, iv_est)
    strategy["optionstrat_url"] = optionstrat_url(meta["symbol"], strategy["legs_raw"])

    operator = ">=" if direction == "BULL" else "<="
    reasons = [
        f"{setup.replace('_', ' ')} in an aligned {'uptrend' if direction == 'BULL' else 'downtrend'}",
        f"trigger is {distance_atr:.2f} ATR from the close",
        f"20-day relative strength versus SPY {rs20*100:+.1f}%",
        f"average dollar volume ${dollar_vol/1e6:.0f}m",
    ]
    if post_event:
        reasons.append(f"possible post-event repricing: {gap_pct:+.1f}% gap on {volume_ratio:.1f}x volume")
    return {
        **meta, "policy_id": POLICY_ID, "cadence": cadence,
        "session": str(last.get("date", today)), "price": round(close, 2),
        "change_pct": round((close / bars[-2]["close"] - 1) * 100, 2),
        "direction": direction, "setup": setup, "score": round(min(score, 100), 1),
        "trigger": {"operator": operator, "price": round(trigger, 2),
                    "label": f"last-hour price {operator} {trigger:.2f}"},
        "invalidation": round(invalidation, 2), "target": round(target, 2),
        "reward_risk": round(rr, 2), "atr": round(atr, 2), "atr_pct": round(atr_pct, 2),
        "features": {"sma20": round(sma20, 2), "sma50": round(sma50, 2),
                     "sma200": round(sma200, 2), "ema21": round(ema21, 2),
                     "rsi14": round(rsi, 1), "adx14": round(adx, 1),
                     "rs20_pct": round(rs20 * 100, 2), "rs60_pct": round(rs60 * 100, 2),
                     "volume_ratio": round(volume_ratio, 2),
                     "dollar_volume": round(dollar_vol), "gap_pct": round(gap_pct, 2)},
        "score_components": {"liquidity": round(liquidity, 1),
                             "trend_structure": round(trend_score, 1),
                             "trigger_readiness": round(trigger_score, 1),
                             "relative_strength": round(rs_score, 1),
                             "momentum_volume": round(momentum, 1),
                             "payoff": round(rr_score, 1), "candle": readiness},
        "post_event_signal": post_event, "earnings": {"status": "VERIFY", "date": None, "dte": None},
        "event_lock": False, "risk_flags": ["Verify the next earnings date before staging."],
        "strategy": strategy, "reasons": reasons,
        "tradingview_url": tradingview_url(meta["symbol"], meta.get("exchange", "NASDAQ")),
    }


def apply_earnings(idea: dict, earnings_date: date | None, today: date) -> dict:
    idea = {**idea, "risk_flags": list(idea.get("risk_flags", []))}
    idea["risk_flags"] = [x for x in idea["risk_flags"] if not x.startswith("Verify the next earnings")]
    if earnings_date is None:
        idea["earnings"] = {"status": "VERIFY", "date": None, "dte": None}
        idea["risk_flags"].append("Verify the next earnings date before staging.")
        return idea
    dte = (earnings_date - today).days
    if dte < 0:
        status = "POST_EVENT"
    elif dte <= 7:
        status = "LOCKED"
    elif dte <= 35:
        status = "INSIDE_HOLD"
    else:
        status = "CLEAR"
    idea["earnings"] = {"status": status, "date": earnings_date.isoformat(), "dte": dte}
    idea["event_lock"] = status == "LOCKED"
    if status == "LOCKED":
        idea["risk_flags"].append("Earnings inside seven days: wait for the post-event trigger reset.")
    elif status == "INSIDE_HOLD":
        idea["risk_flags"].append("Earnings falls inside the 30-day hold: mandatory pre-event exit unless explicitly event-sized.")
    return idea


def rank_ideas(ideas: list[dict], cadence: str, previous_symbols: set[str] | None = None,
               limit: int | None = None) -> list[dict]:
    cadence_cap = WEEKLY_LIMIT if cadence == "weekly" else DAILY_LIMIT
    limit = min(max(int(limit or cadence_cap), 1), cadence_cap)
    previous_symbols = previous_symbols or set()
    for idea in ideas:
        idea["raw_score"] = idea["score"]
        if idea["symbol"] in previous_symbols:
            idea["score"] = round(min(100, idea["score"] + 2), 1)
            idea["score_components"]["watchlist_stability"] = 2.0
        if idea.get("event_lock"):
            idea["score"] = round(max(0, idea["score"] - 25), 1)
    ordered = sorted(ideas, key=lambda x: (x["score"], x["features"]["dollar_volume"]), reverse=True)
    selected, sectors, clusters = [], {}, {}
    for idea in ordered:
        # A locked earnings name cannot become executable during this
        # watchlist's life, so do not let it consume one of only 5–10 slots.
        if idea.get("event_lock"):
            continue
        sector, cluster = idea.get("sector", "Other"), idea.get("cluster", idea["symbol"])
        if sectors.get(sector, 0) >= MAX_PER_SECTOR or clusters.get(cluster, 0) >= MAX_PER_CLUSTER:
            continue
        selected.append(idea)
        sectors[sector] = sectors.get(sector, 0) + 1
        clusters[cluster] = clusters.get(cluster, 0) + 1
        if len(selected) >= limit:
            break
    for i, idea in enumerate(selected, 1):
        idea["rank"] = i
    return selected


def trigger_state(idea: dict, live_price: float, *, fresh: bool,
                  in_last_hour: bool) -> dict:
    trigger = float(idea["trigger"]["price"])
    atr = max(float(idea.get("atr") or 0), live_price * 0.01)
    direction = idea["direction"]
    crossed = live_price >= trigger if direction == "BULL" else live_price <= trigger
    distance = (trigger - live_price) if direction == "BULL" else (live_price - trigger)
    distance_atr = distance / atr
    overrun_atr = max(0.0, -distance_atr)
    invalidation = float(idea.get("invalidation") or 0)
    invalid = ((direction == "BULL" and invalidation and live_price <= invalidation) or
               (direction == "BEAR" and invalidation and live_price >= invalidation))
    if idea.get("event_lock"):
        state, reason = "LOCKED", "Company earnings gate is active."
    elif not fresh:
        state, reason = "STALE", "Quote is stale; no alert or staging."
    elif invalid:
        state, reason = "INVALID", "The setup invalidation level has been breached."
    elif crossed and overrun_atr > 0.35:
        state, reason = "EXTENDED", "Price is more than 0.35 ATR beyond the trigger; do not chase."
    elif crossed and in_last_hour:
        state, reason = "TRIGGERED", "Price trigger crossed inside 15:00–15:40 ET."
    elif crossed:
        state, reason = "CROSSED", "Price crossed outside the standard execution window."
    elif distance_atr <= 0.35:
        state, reason = "ARMED", "Within 0.35 ATR of the trigger."
    else:
        state, reason = "WAIT", "Trigger has not fired."
    return {"state": state, "reason": reason, "live_price": round(live_price, 2),
            "distance": round(distance, 2), "distance_atr": round(distance_atr, 2),
            "overrun_atr": round(overrun_atr, 2),
            "flash": state == "TRIGGERED"}


def model_card(idea: dict, today: date, spot: float | None = None) -> dict:
    spot = float(spot or idea["price"])
    raw = idea["strategy"]["legs_raw"]
    legs = [Leg(cp=x["cp"], strike=float(x["strike"]),
                expiry=date.fromisoformat(x["expiry"]), qty=int(x["qty"]),
                iv=float(x.get("iv") or 0.30)) for x in raw]
    entry = round(struct_value(spot, legs, today), 2)
    profile = risk_profile(spot, legs, today, entry=entry)
    greeks = {k: round(v * 100, 2) for k, v in struct_greeks(spot, legs, today).items()}
    return {
        "strategy": "debit_spread", "label": idea["strategy"]["label"],
        "legs_raw": raw, "legs": [x.key() for x in legs], "net_mid": entry,
        "mid_src": "model", "greeks": greeks, "risk_profile": profile,
        "max_profit": profile["max_profit"], "max_loss": profile["max_loss"],
        "breakevens": profile["breakevens"],
        "cash_required": round(abs(profile["max_loss"]) * 100, 2),
        "optionstrat_url": optionstrat_url(idea["symbol"], raw),
        "management": {
            "max_hold": "30 calendar days",
            "time_stop": "Exit by 10 DTE, 30 calendar days, or the session before earnings — whichever comes first.",
            "thesis_stop": f"Underlying closes through {idea['invalidation']:.2f} invalidates the setup.",
            "profit_review": "Review at 50% of modeled maximum profit; do not hold a nearly-maxed vertical for the final increment.",
        },
        "tws_stage_allowed": False, "permitted": False, "blocks": ["live repricing required"],
        "policy_id": POLICY_ID, "rationale": list(idea["reasons"]),
        "governor": {"approved_lots": 0, "risk_approved": False},
    }
