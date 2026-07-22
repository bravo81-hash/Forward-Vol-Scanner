"""Causal daily-chart pattern geometry.

The detectors deliberately return *candidates*, not trades.  They use only bars
available at the evaluation date, expose the trigger/invalidation explicitly,
and keep confirmation separate from geometry quality.  Context (momentum,
relative strength, volume, market and sector) is applied by pattern_scanner.py.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

import numpy as np
import pandas as pd

from . import indicators as ind


@dataclass
class PatternCandidate:
    ticker: str
    code: str
    pattern: str
    side: str
    geometry_score: float
    trigger: float
    invalidation: float
    target: float
    start_bar: int
    end_bar: int
    status: str = "FORMING"
    distance_atr: float | None = None
    breakout_age: int | None = None
    breakout_date: str | None = None
    bars_since_completion: int | None = None
    start_date: str | None = None
    end_date: str | None = None
    volume_confirmed: bool = False
    detail: str = ""
    points: dict = field(default_factory=dict)
    setup_family: str = "structural"
    timeframe: str = "1D"
    entry_rule: str = "Enter only after a completed daily close confirms the trigger"
    stop_basis: str = "intraday"
    initial_stop: float | None = None
    conservative_target: float | None = None
    stretch_target: float | None = None
    target_basis: str = "measured move"
    max_hold_bars: int | None = None
    exit_rule: str = "Exit at target, invalidation, or time limit"
    prior_trend_pct: float | None = None
    formation_bars: int | None = None
    volume_pattern: str | None = None

    def row(self) -> dict:
        def native(value):
            if isinstance(value, dict):
                return {key: native(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [native(item) for item in value]
            if isinstance(value, np.integer):
                return int(value)
            if isinstance(value, np.floating):
                return float(value)
            if isinstance(value, (pd.Timestamp, np.datetime64)):
                return pd.Timestamp(value).date().isoformat()
            return value

        return native(asdict(self))


def _clip(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def _atr(d: pd.DataFrame) -> float:
    x = float(ind.atr(d).iloc[-1])
    return x if np.isfinite(x) and x > 0 else 0.0


def _pivots(d: pd.DataFrame, left: int = 3, right: int = 3):
    """Confirmed integer-index pivots; the final ``right`` bars are excluded."""
    h, low = d["high"].to_numpy(float), d["low"].to_numpy(float)
    ph, pl = [], []
    for i in range(left, len(d) - right):
        wh, wl = h[i - left : i + right + 1], low[i - left : i + right + 1]
        if h[i] == np.max(wh) and int(np.argmax(wh)) == left:
            ph.append((i, float(h[i])))
        if low[i] == np.min(wl) and int(np.argmin(wl)) == left:
            pl.append((i, float(low[i])))
    return ph, pl


def _between(points: Iterable[tuple[int, float]], lo: int, hi: int):
    return [(i, p) for i, p in points if lo < i < hi]


def _prior_trend(
    d: pd.DataFrame,
    start_bar: int,
    side: str,
    atr: float,
    min_pct: float = 0.04,
    lookback: int = 50,
) -> tuple[bool, float]:
    """Require a meaningful trend before a reversal/continuation formation.

    ``side`` describes the required prior trend, not the eventual trade: use
    ``down`` before bullish reversals and ``up`` before bearish reversals.
    A percent move and an ATR move are both required so a visually flat series
    cannot qualify merely because its price is high or low.
    """
    end = max(1, min(int(start_bar), len(d) - 1))
    start = max(0, end - lookback)
    x = d["close"].iloc[start : end + 1].astype(float)
    if len(x) < 15 or atr <= 0:
        return False, 0.0
    move = float(x.iloc[-1] / x.iloc[0] - 1)
    atr_move = abs(float(x.iloc[-1] - x.iloc[0])) / atr
    slope = float(np.polyfit(np.arange(len(x)), x.to_numpy(), 1)[0])
    direction_ok = move < 0 and slope < 0 if side == "down" else move > 0 and slope > 0
    return bool(direction_ok and abs(move) >= min_pct and atr_move >= 1.5), move


def _body_ok(row: pd.Series, minimum: float = 0.20) -> bool:
    """Exclude doji-like bars from two-bar swing signals."""
    spread = float(row["high"] - row["low"])
    body = abs(float(row["close"] - row["open"]))
    return bool(spread > 0 and body / spread >= minimum)


def _breakout_info(
    close: np.ndarray, trigger: float, side: str, penetration: float, start_bar: int = 0
) -> tuple[int | None, int | None]:
    """Age and bar of the latest *crossing event* during this pattern.

    Earlier history often traded beyond today's trigger (notably before a cup,
    double bottom, or reversal).  Counting those bars would mislabel an
    unfinished setup as a retest, so breakout history starts at the candidate's
    first pattern bar.
    """
    start = max(0, min(int(start_bar), len(close) - 1))
    window = close[start:]
    beyond = (
        window > trigger + penetration
        if side == "long"
        else window < trigger - penetration
    )
    # Counting every bar beyond the trigger resets age to zero forever.  A
    # breakout is the transition from not-beyond to beyond.
    crosses = np.where(beyond & ~np.r_[False, beyond[:-1]])[0]
    if not len(crosses):
        return None, None
    bar = start + int(crosses[-1])
    return int(len(close) - 1 - bar), bar


def classify(
    candidate: PatternCandidate,
    d: pd.DataFrame,
    atr: float,
    near_atr: float = 1.0,
    penetration_atr: float = 0.25,
    retest_atr: float = 0.35,
    max_confirm_age: int = 3,
    max_retest_age: int = 10,
    max_extension_atr: float = 2.0,
) -> PatternCandidate:
    """Attach a mutually-exclusive setup state using daily closes only.

    ``TRIGGERED_INTRADAY`` is reserved for the TWS live overlay.  A one-cent
    breach is never a close confirmation: the default penetration is 0.25 ATR.
    """
    if atr <= 0:
        return candidate
    close = d["close"].to_numpy(float)
    last = float(close[-1])
    vol = d["volume"].to_numpy(float)
    vol_avg = (
        float(np.nanmean(vol[-21:-1])) if len(vol) > 21 else float(np.nanmean(vol))
    )
    candidate.volume_confirmed = bool(vol_avg > 0 and vol[-1] >= 1.2 * vol_avg)
    pen, ret = penetration_atr * atr, retest_atr * atr
    candidate.start_date = pd.Timestamp(d.index[candidate.start_bar]).date().isoformat()
    candidate.end_date = pd.Timestamp(d.index[candidate.end_bar]).date().isoformat()
    candidate.bars_since_completion = len(d) - 1 - candidate.end_bar
    candidate.formation_bars = (
        candidate.formation_bars or candidate.end_bar - candidate.start_bar + 1
    )
    if candidate.initial_stop is None:
        candidate.initial_stop = round(
            float(
                candidate.trigger - atr
                if candidate.side == "long"
                else candidate.trigger + atr
            ),
            4,
        )
    if candidate.conservative_target is None:
        candidate.conservative_target = candidate.target
    if candidate.setup_family == "swing_reversion":
        candidate.breakout_age = 0
        candidate.breakout_date = pd.Timestamp(d.index[-1]).date().isoformat()
        candidate.distance_atr = 0.0
        candidate.status = "CLOSE_CONFIRMED"
        candidate.volume_confirmed = False
        return candidate
    if candidate.side == "long":
        valid_order = candidate.invalidation < candidate.trigger < candidate.target
    else:
        valid_order = candidate.target < candidate.trigger < candidate.invalidation
    if not valid_order or abs(candidate.trigger - candidate.invalidation) < 0.5 * atr:
        candidate.status = "INVALID"
        return candidate

    age, breakout_bar = _breakout_info(
        close, candidate.trigger, candidate.side, pen, candidate.start_bar
    )
    candidate.breakout_age = age
    if breakout_bar is not None:
        candidate.breakout_date = pd.Timestamp(d.index[breakout_bar]).date().isoformat()

    target_hit = False
    if breakout_bar is not None:
        after = d.iloc[breakout_bar:]
        target_hit = (
            float(after["high"].max()) >= candidate.target
            if candidate.side == "long"
            else float(after["low"].min()) <= candidate.target
        )

    if candidate.side == "long":
        candidate.distance_atr = round((candidate.trigger - last) / atr, 2)
        extension = (last - candidate.trigger) / atr
        if (
            target_hit
            or extension > max_extension_atr
            or (age is not None and age > max_retest_age)
        ):
            status = "EXPIRED"
        elif last < candidate.invalidation:
            status = "FAILED"
        elif (
            age is not None
            and 0 < age <= max_retest_age
            and candidate.trigger - ret <= last <= candidate.trigger + ret
        ):
            status = "RETESTING"
        elif age is not None and age > max_confirm_age:
            status = "EXPIRED"
        elif (
            age is not None
            and age <= max_confirm_age
            and last > candidate.trigger + pen
        ):
            status = "CLOSE_CONFIRMED"
        elif abs(candidate.trigger - last) <= near_atr * atr:
            status = "NEAR_TRIGGER"
        else:
            status = "FORMING"
    else:
        candidate.distance_atr = round((last - candidate.trigger) / atr, 2)
        extension = (candidate.trigger - last) / atr
        if (
            target_hit
            or extension > max_extension_atr
            or (age is not None and age > max_retest_age)
        ):
            status = "EXPIRED"
        elif last > candidate.invalidation:
            status = "FAILED"
        elif (
            age is not None
            and 0 < age <= max_retest_age
            and candidate.trigger - ret <= last <= candidate.trigger + ret
        ):
            status = "RETESTING"
        elif age is not None and age > max_confirm_age:
            status = "EXPIRED"
        elif (
            age is not None
            and age <= max_confirm_age
            and last < candidate.trigger - pen
        ):
            status = "CLOSE_CONFIRMED"
        elif abs(last - candidate.trigger) <= near_atr * atr:
            status = "NEAR_TRIGGER"
        else:
            status = "FORMING"
    candidate.status = status
    return candidate


def _candidate(
    ticker,
    code,
    name,
    side,
    score,
    trigger,
    invalidation,
    target,
    start,
    end,
    detail,
    points,
    d,
    atr,
    **plan,
):
    c = PatternCandidate(
        ticker,
        code,
        name,
        side,
        round(_clip(score), 3),
        round(float(trigger), 4),
        round(float(invalidation), 4),
        round(float(target), 4),
        int(start),
        int(end),
        detail=detail,
        points=points,
        **plan,
    )
    # Make every detector auditable in the UI.  Existing detector point values
    # are mapped to the nearest bar inside the formation and emitted with dates.
    enriched = {}
    span = d.iloc[max(0, int(start)) : min(len(d), int(end) + 1)]
    for label, value in points.items():
        exact_bar = None
        if isinstance(value, (tuple, list)) and len(value) == 2:
            exact_bar, value = int(value[0]), value[1]
        price = float(value)
        if span.empty:
            continue
        if exact_bar is None:
            delta = pd.concat(
                [
                    (span["high"] - price).abs(),
                    (span["low"] - price).abs(),
                    (span["close"] - price).abs(),
                ],
                axis=1,
            ).min(axis=1)
            stamp = delta.idxmin()
            bar = int(d.index.get_loc(stamp))
        else:
            bar = max(0, min(exact_bar, len(d) - 1))
            stamp = d.index[bar]
        enriched[label] = {
            "bar": bar,
            "date": pd.Timestamp(stamp).date().isoformat(),
            "price": round(price, 4),
        }
    c.points = enriched
    return classify(c, d, atr)


def flat_base(ticker: str, d: pd.DataFrame, atr: float):
    """Flat base / VCP: prior advance, repeated ceiling and contracting range."""
    if len(d) < 90 or atr <= 0:
        return None
    best = None
    for w in (25, 35, 50, 65):
        x = d.tail(w)
        h, low, c = (
            x["high"].to_numpy(float),
            x["low"].to_numpy(float),
            x["close"].to_numpy(float),
        )
        trigger = float(np.quantile(h, 0.90))
        floor = float(np.quantile(low, 0.10))
        mid = float(np.mean(c))
        width = (trigger - floor) / mid if mid else 1.0
        if not (0.035 <= width <= 0.22):
            continue
        tol = max(0.012 * trigger, 0.45 * atr)
        touches = int(np.sum(np.abs(h - trigger) <= tol))
        if touches < 2:
            continue
        half = max(7, w // 2)
        r1 = float(np.max(h[:half]) - np.min(low[:half]))
        r2 = float(np.max(h[-half:]) - np.min(low[-half:]))
        contract = 1 - r2 / r1 if r1 > 0 else 0
        prior = d.iloc[-w - 60 : -w]["close"]
        prior_ret = (
            float(prior.iloc[-1] / prior.iloc[0] - 1) if len(prior) > 20 else 0.0
        )
        if prior_ret < 0.05 or contract < 0.05:
            continue
        compression = _clip(contract / 0.50)
        score = (
            0.30 * _clip(touches / 4)
            + 0.30 * compression
            + 0.20 * _clip(prior_ret / 0.25)
            + 0.20 * _clip((0.22 - width) / 0.18)
        )
        start = len(d) - w
        cand = _candidate(
            ticker,
            "P1",
            "Flat base / VCP",
            "long",
            score,
            trigger,
            floor - 0.25 * atr,
            trigger + (trigger - floor),
            start,
            len(d) - 1,
            f"{w}d base; {touches} resistance tests; range contracted {contract:.0%}",
            {"resistance": trigger, "support": floor},
            d,
            atr,
            initial_stop=round(trigger - atr, 4),
            target_basis="next major resistance preferred; base-height measured move fallback",
            max_hold_bars=max(5, w // 2),
            exit_rule="Exit if the target is not reached within half the base duration",
            prior_trend_pct=round(prior_ret * 100, 2),
            volume_pattern="Prefer contracting base volume and expanding breakout volume",
        )
        if best is None or cand.geometry_score > best.geometry_score:
            best = cand
    return best


def cup_handle(ticker: str, d: pd.DataFrame, atr: float):
    """Rounded cup with a shallow handle in the upper half."""
    if len(d) < 150 or atr <= 0:
        return None
    best = None
    for w in (90, 120, 160, 200):
        if len(d) < w + 30:
            continue
        x = d.tail(w)
        c = x["close"].to_numpy(float)
        n = len(c)
        left = int(np.argmax(c[: max(20, n // 3)]))
        trough = (
            left + 5 + int(np.argmin(c[left + 5 : int(n * 0.78)]))
            if int(n * 0.78) > left + 5
            else left
        )
        if trough <= left + 5:
            continue
        right_start = trough + 8
        if right_start >= n - 8:
            continue
        right = right_start + int(np.argmax(c[right_start : n - 5]))
        rim1, bottom, rim2 = c[left], c[trough], c[right]
        rim = min(rim1, rim2)
        depth = (max(rim1, rim2) - bottom) / max(rim1, rim2)
        symmetry = abs(rim1 - rim2) / max(rim1, rim2)
        if not (0.10 <= depth <= 0.42 and symmetry <= 0.10):
            continue
        handle = x.iloc[right:]
        if not (5 <= len(handle) <= 30):
            continue
        handle_low = float(handle["low"].min())
        handle_low_bar = len(d) - w + int(handle["low"].argmin()) + right
        handle_high = float(handle["high"].max())
        handle_depth = (rim - handle_low) / rim
        if handle_depth < -0.03 or handle_depth > min(0.18, depth * 0.55):
            continue
        # A rounded bottom spends time near the low; a one-bar V does not.
        near_bottom = int(np.sum(c <= bottom + 0.25 * (rim - bottom)))
        if near_bottom < max(4, int(0.08 * w)):
            continue
        prior = d.iloc[-w - 50 : -w]["close"]
        prior_ret = (
            float(prior.iloc[-1] / prior.iloc[0] - 1) if len(prior) > 20 else 0.0
        )
        if prior_ret < 0:
            continue
        trigger = max(rim1, rim2, handle_high)
        score = (
            0.28 * _clip(1 - symmetry / 0.10)
            + 0.25 * _clip(near_bottom / (0.18 * w))
            + 0.25 * _clip(1 - handle_depth / 0.18)
            + 0.22 * _clip(prior_ret / 0.25)
        )
        start = len(d) - w + left
        cand = _candidate(
            ticker,
            "P2",
            "Cup and handle",
            "long",
            score,
            trigger,
            handle_low - 0.25 * atr,
            trigger + (trigger - bottom),
            start,
            handle_low_bar,
            f"cup depth {depth:.0%}; handle {handle_depth:.0%}; rim gap {symmetry:.1%}",
            {
                "left_rim": (len(d) - w + left, rim1),
                "bottom": (len(d) - w + trough, bottom),
                "right_rim": (len(d) - w + right, rim2),
                "handle_low": (handle_low_bar, handle_low),
            },
            d,
            atr,
        )
        if best is None or cand.geometry_score > best.geometry_score:
            best = cand
    return best


def _three_pivot_reversal(ticker, d, atr, bullish=True):
    """Inverse/ordinary head-and-shoulders from three alternating pivots."""
    ph, pl = _pivots(d.tail(180))
    off = len(d) - min(len(d), 180)
    swings = pl if bullish else ph
    opposite = ph if bullish else pl
    if len(swings) < 3:
        return None
    best = None
    for a in range(max(0, len(swings) - 7), len(swings) - 2):
        s1, s2, s3 = swings[a : a + 3]
        if not (10 <= s2[0] - s1[0] <= 65 and 10 <= s3[0] - s2[0] <= 65):
            continue
        shoulder_gap = abs(s1[1] - s3[1]) / ((s1[1] + s3[1]) / 2)
        if shoulder_gap > 0.08:
            continue
        if bullish:
            head_prom = (min(s1[1], s3[1]) - s2[1]) / min(s1[1], s3[1])
            if head_prom < 0.035:
                continue
        else:
            head_prom = (s2[1] - max(s1[1], s3[1])) / max(s1[1], s3[1])
            if head_prom < 0.035:
                continue
        prior_ok, prior_move = _prior_trend(
            d, off + s1[0], "down" if bullish else "up", atr
        )
        if not prior_ok:
            continue
        left_neck = _between(opposite, s1[0], s2[0])
        right_neck = _between(opposite, s2[0], s3[0])
        if not left_neck or not right_neck:
            continue
        p1 = (
            max(left_neck, key=lambda z: z[1])
            if bullish
            else min(left_neck, key=lambda z: z[1])
        )
        p2 = (
            max(right_neck, key=lambda z: z[1])
            if bullish
            else min(right_neck, key=lambda z: z[1])
        )
        slope = (p2[1] - p1[1]) / max(1, p2[0] - p1[0])
        trigger = p2[1] + slope * max(0, (len(d.tail(180)) - 1 - p2[0]))
        height = abs(trigger - s2[1])
        if height < 2 * atr:
            continue
        symmetry = 1 - abs((s2[0] - s1[0]) - (s3[0] - s2[0])) / max(s3[0] - s1[0], 1)
        score = (
            0.35 * _clip(head_prom / 0.15)
            + 0.30 * _clip(1 - shoulder_gap / 0.08)
            + 0.35 * _clip(symmetry)
        )
        duration = s3[0] - s1[0] + 1
        score = 0.88 * score + 0.12 * _clip(duration / 100)
        if bullish:
            cand = _candidate(
                ticker,
                "P3",
                "Inverse head and shoulders",
                "long",
                score,
                trigger,
                min(s1[1], s3[1]) - 0.25 * atr,
                trigger + height,
                off + s1[0],
                off + s3[0],
                f"head {head_prom:.1%} below shoulders; neckline slope {slope:.3f}/bar",
                {
                    "left_shoulder": (off + s1[0], s1[1]),
                    "head": (off + s2[0], s2[1]),
                    "right_shoulder": (off + s3[0], s3[1]),
                    "neckline_left": (off + p1[0], p1[1]),
                    "neckline_right": (off + p2[0], p2[1]),
                },
                d,
                atr,
                initial_stop=round(trigger - atr, 4),
                target_basis="next major resistance preferred; head-to-neckline measured move fallback",
                max_hold_bars=max(5, duration // 2),
                exit_rule="Trail by 1 ATR after the preferred target; exit on invalidation or time limit",
                prior_trend_pct=round(prior_move * 100, 2),
                volume_pattern="Prefer lighter right-shoulder selling and expanding breakout volume",
            )
        else:
            cand = _candidate(
                ticker,
                "P6",
                "Head and shoulders top",
                "short",
                score,
                trigger,
                max(s1[1], s3[1]) + 0.25 * atr,
                trigger - height,
                off + s1[0],
                off + s3[0],
                f"head {head_prom:.1%} above shoulders; neckline slope {slope:.3f}/bar",
                {
                    "left_shoulder": (off + s1[0], s1[1]),
                    "head": (off + s2[0], s2[1]),
                    "right_shoulder": (off + s3[0], s3[1]),
                    "neckline_left": (off + p1[0], p1[1]),
                    "neckline_right": (off + p2[0], p2[1]),
                },
                d,
                atr,
                initial_stop=round(trigger + atr, 4),
                target_basis="next major support preferred; head-to-neckline measured move fallback",
                max_hold_bars=max(5, duration // 2),
                exit_rule="Trail by 1 ATR after the preferred target; exit on invalidation or time limit",
                prior_trend_pct=round(prior_move * 100, 2),
                volume_pattern="Volume is supporting evidence, not a mandatory H&S rule",
            )
        if best is None or cand.geometry_score > best.geometry_score:
            best = cand
    return best


def inverse_head_shoulders(ticker, d, atr):
    return _three_pivot_reversal(ticker, d, atr, True)


def head_shoulders_top(ticker, d, atr):
    return _three_pivot_reversal(ticker, d, atr, False)


def _double(ticker, d, atr, bullish=True):
    x = d.tail(160)
    ph, pl = _pivots(x)
    swings = pl if bullish else ph
    opposite = ph if bullish else pl
    if len(swings) < 2:
        return None
    off, best = len(d) - len(x), None
    recent = swings[-7:]
    for s1, s2 in zip(recent, recent[1:]):
        sep = s2[0] - s1[0]
        if not 10 <= sep <= 90:
            continue
        similarity = abs(s1[1] - s2[1]) / ((s1[1] + s2[1]) / 2)
        if similarity > 0.055:
            continue
        mids = _between(opposite, s1[0], s2[0])
        if not mids:
            continue
        mid = (
            max(mids, key=lambda z: z[1]) if bullish else min(mids, key=lambda z: z[1])
        )
        base = (s1[1] + s2[1]) / 2
        depth = abs(mid[1] - base) / base
        if depth < 0.055 or abs(mid[1] - base) < 2 * atr:
            continue
        prior_ok, prior_move = _prior_trend(
            d, off + s1[0], "down" if bullish else "up", atr
        )
        if not prior_ok:
            continue
        # Broad/rounded pivots score above isolated single-bar spikes.
        center = off + s2[0]
        local = d.iloc[max(0, center - 3) : min(len(d), center + 4)]
        if bullish:
            broad = int(np.sum(local["low"] <= s2[1] + 0.5 * atr))
            invalid = min(s1[1], s2[1]) - 0.25 * atr
            target = mid[1] + (mid[1] - base)
            code, name, side = "P4", "Double bottom", "long"
        else:
            broad = int(np.sum(local["high"] >= s2[1] - 0.5 * atr))
            invalid = max(s1[1], s2[1]) + 0.25 * atr
            # A half-depth objective is the conservative trade-management
            # target; retain the full measured move as the stretch objective.
            target = mid[1] - 0.5 * (base - mid[1])
            code, name, side = "P7", "Double top", "short"
        v1 = float(d["volume"].iloc[max(0, off + s1[0] - 2) : off + s1[0] + 3].mean())
        v2 = float(d["volume"].iloc[max(0, off + s2[0] - 2) : off + s2[0] + 3].mean())
        volume_ratio = v2 / v1 if v1 > 0 else 1.0
        score = (
            0.38 * _clip(1 - similarity / 0.055)
            + 0.32 * _clip(depth / 0.18)
            + 0.30 * _clip(broad / 4)
        )
        if bullish:
            score = 0.92 * score + 0.08 * _clip((1.10 - volume_ratio) / 0.40)
        duration = sep + 1
        full_move = abs(mid[1] - base)
        cand = _candidate(
            ticker,
            code,
            name,
            side,
            score,
            mid[1],
            invalid,
            target,
            off + s1[0],
            off + s2[0],
            f"peaks/troughs {similarity:.1%} apart; {sep} bars; depth {depth:.0%}",
            {
                "first": (off + s1[0], s1[1]),
                "middle": (off + mid[0], mid[1]),
                "second": (off + s2[0], s2[1]),
            },
            d,
            atr,
            initial_stop=round(mid[1] - atr if bullish else mid[1] + atr, 4),
            conservative_target=round(target, 4),
            stretch_target=round(
                mid[1] + 2 * full_move if bullish else mid[1] - full_move, 4
            ),
            target_basis=(
                "next major resistance preferred; one-depth measured target, two-depth stretch"
                if bullish
                else "next major support preferred; half-depth conservative target, full-depth stretch"
            ),
            max_hold_bars=max(5, duration // 2 if not bullish else duration),
            exit_rule=(
                "Stay through an orderly pullback; exit at target, invalidation, or proportional time limit"
                if bullish
                else "Double tops should resolve promptly; exit if no progress within half the formation time"
            ),
            prior_trend_pct=round(prior_move * 100, 2),
            volume_pattern=(
                f"second-bottom volume was {volume_ratio:.2f}x first-bottom volume; lower is preferred"
                if bullish
                else "Volume is not a mandatory double-top criterion"
            ),
        )
        if best is None or cand.geometry_score > best.geometry_score:
            best = cand
    return best


def double_bottom(ticker, d, atr):
    return _double(ticker, d, atr, True)


def double_top(ticker, d, atr):
    return _double(ticker, d, atr, False)


def ascending_triangle(ticker, d, atr):
    """Horizontal ceiling plus a chronological sequence of rising lows.

    The structure must finish near the current bar.  Lows are taken only from
    the intervals between/after the selected ceiling tests, preventing an old
    resistance cluster from being combined with unrelated post-breakout lows.
    """
    if len(d) < 100 or atr <= 0:
        return None
    best = None
    for w in (35, 50, 70, 90):
        x = d.tail(w)
        prior = d.iloc[-w - 60 : -w]["close"]
        prior_ret = (
            float(prior.iloc[-1] / prior.iloc[0] - 1) if len(prior) >= 20 else 0.0
        )
        if prior_ret < 0.05:
            continue
        ph, pl = _pivots(x, 2, 2)
        if len(ph) < 3 or len(pl) < 3:
            continue
        for end_i in range(2, len(ph)):
            tests = ph[max(0, end_i - 3) : end_i + 1]
            if len(tests) < 3:
                continue
            resistance = float(np.median([p for _, p in tests]))
            tol = max(0.20 * atr, min(0.008 * resistance, 0.35 * atr))
            tests = [(i, p) for i, p in tests if abs(p - resistance) <= tol]
            if len(tests) < 3:
                continue
            # One trough between each ceiling test and one after the last.
            lows = []
            for left, right in zip(tests, tests[1:]):
                interval = _between(pl, left[0], right[0])
                if not interval:
                    lows = []
                    break
                lows.append(min(interval, key=lambda z: z[1]))
            after = [(i, p) for i, p in pl if tests[-1][0] < i <= tests[-1][0] + 20]
            if not lows or not after:
                continue
            lows.append(min(after, key=lambda z: z[1]))
            values = np.asarray([p for _, p in lows], dtype=float)
            if len(values) < 3 or np.any(np.diff(values) < 0.20 * atr):
                continue
            rise = (values[-1] - values[0]) / max(values[0], 1e-9)
            height = resistance - values[0]
            first_gap, last_gap = resistance - values[0], resistance - values[-1]
            if rise < 0.02 or height < 2 * atr or last_gap > 0.75 * first_gap:
                continue
            # No close may materially break the ceiling before the final low;
            # that would contaminate the formation with post-breakout pivots.
            prebreak = x["close"].iloc[tests[0][0] : lows[-1][0] + 1]
            if bool((prebreak > resistance + 0.25 * atr).any()):
                continue
            dispersion = float(np.std([p for _, p in tests]) / resistance)
            if dispersion > 0.008:
                continue
            score = (
                0.30 * _clip(len(tests) / 4)
                + 0.35 * _clip(rise / 0.12)
                + 0.20 * _clip(1 - dispersion / 0.008)
                + 0.15 * _clip(1 - last_gap / first_gap)
            )
            off = len(d) - w
            cand = _candidate(
                ticker,
                "P5",
                "Ascending triangle",
                "long",
                score,
                resistance,
                values[-1] - 0.25 * atr,
                resistance + height,
                off + min(tests[0][0], lows[0][0]),
                off + lows[-1][0],
                f"{len(tests)} ceiling tests; {len(lows)} consecutive rising lows; lows rose {rise:.1%}",
                {
                    **{
                        f"ceiling_{j + 1}": (off + i, p)
                        for j, (i, p) in enumerate(tests)
                    },
                    **{
                        f"rising_low_{j + 1}": (off + i, p)
                        for j, (i, p) in enumerate(lows)
                    },
                },
                d,
                atr,
                initial_stop=round(resistance - atr, 4),
                target_basis="next major resistance preferred; perpendicular triangle height fallback",
                max_hold_bars=max(5, (lows[-1][0] - tests[0][0] + 1) // 2),
                exit_rule="Exit if the objective is not reached within half the formation duration",
                prior_trend_pct=round(prior_ret * 100, 2),
                volume_pattern="Prefer contracting consolidation volume and expanding breakout volume",
            )
            if best is None or cand.geometry_score > best.geometry_score:
                best = cand
    return best


def flag(ticker, d, atr):
    """Tight bull/bear flag after a strong, non-climactic impulse."""
    if len(d) < 90 or atr <= 0:
        return None
    best = None
    c, v = d["close"].to_numpy(float), d["volume"].to_numpy(float)
    for breakout_tail in (0, 1, 2):
        for flag_n in range(5, 16):
            for pole_n in (5, 8, 12, 15):
                end = len(d) - flag_n - breakout_tail
                start = end - pole_n
                if start < 20:
                    continue
                pole = c[end - 1] - c[start]
                pole_atr = abs(pole) / atr
                pole_pct = abs(pole) / c[start]
                if pole_atr < 2.5 or pole_pct < 0.07 or pole_atr > 9:
                    continue
                side = "long" if pole > 0 else "short"
                fx = d.iloc[end : end + flag_n]
                flag_move = float(fx["close"].iloc[-1] - fx["close"].iloc[0])
                retrace = abs(flag_move / pole) if pole else 1.0
                if (
                    retrace > 0.52
                    or (side == "long" and flag_move > 0.35 * atr)
                    or (side == "short" and flag_move < -0.35 * atr)
                ):
                    continue
                flag_vol = float(np.mean(v[end:]))
                pole_vol = float(np.mean(v[start:end]))
                vol_contract = 1 - flag_vol / pole_vol if pole_vol > 0 else 0
                if vol_contract < -0.10:
                    continue
                if side == "long":
                    trigger, invalid = (
                        float(fx["high"].max()),
                        float(fx["low"].min()) - 0.25 * atr,
                    )
                    target = trigger + abs(pole)
                    code, name = "P8", "Bull flag"
                else:
                    trigger, invalid = (
                        float(fx["low"].min()),
                        float(fx["high"].max()) + 0.25 * atr,
                    )
                    target = trigger - abs(pole)
                    code, name = "P9", "Bear flag"
                score = (
                    0.35 * _clip((pole_atr - 2.5) / 4)
                    + 0.35 * _clip(1 - retrace / 0.52)
                    + 0.30 * _clip((vol_contract + 0.1) / 0.5)
                )
                cand = _candidate(
                    ticker,
                    code,
                    name,
                    side,
                    score,
                    trigger,
                    invalid,
                    target,
                    start,
                    end + flag_n - 1,
                    f"{pole_n}d pole {pole_pct:.0%}; {flag_n}d flag; retrace {retrace:.0%}; volume {vol_contract:.0%} lower",
                    {
                        "pole_start": (start, round(c[start], 2)),
                        "pole_end": (end - 1, round(c[end - 1], 2)),
                    },
                    d,
                    atr,
                    initial_stop=round(
                        trigger - atr if side == "long" else trigger + atr, 4
                    ),
                    target_basis="next major support/resistance preferred; pole-length measured move fallback",
                    max_hold_bars=max(5, flag_n),
                    exit_rule="Exit on invalidation or if the flag fails to resolve within its own duration",
                    prior_trend_pct=round((pole / c[start]) * 100, 2),
                    volume_pattern=f"flag volume contracted {vol_contract:.0%} versus the pole",
                )
                if best is None or cand.geometry_score > best.geometry_score:
                    best = cand
    return best


def bearish_rectangle(ticker: str, d: pd.DataFrame, atr: float):
    """Bearish continuation rectangle after a measurable downtrend."""
    if len(d) < 90 or atr <= 0:
        return None
    best = None
    for breakout_tail in (0, 1, 2):
        for w in (20, 30, 40, 55):
            end_bar = len(d) - 1 - breakout_tail
            start = end_bar - w + 1
            if start < 20:
                continue
            x = d.iloc[start : end_bar + 1]
            h = x["high"].to_numpy(float)
            low = x["low"].to_numpy(float)
            c = x["close"].to_numpy(float)
            ceiling = float(np.quantile(h, 0.90))
            trigger = float(np.quantile(low, 0.10))
            width = (ceiling - trigger) / max(float(np.mean(c)), 1e-9)
            if not 0.035 <= width <= 0.20:
                continue
            tol = max(0.20 * atr, min(0.008 * trigger, 0.40 * atr))
            floor_tests = int(np.sum(np.abs(low - trigger) <= tol))
            ceiling_tests = int(np.sum(np.abs(h - ceiling) <= tol))
            if floor_tests < 2 or ceiling_tests < 2:
                continue
            prior = d.iloc[max(0, start - 60) : start]["close"]
            prior_ret = (
                float(prior.iloc[-1] / prior.iloc[0] - 1) if len(prior) >= 20 else 0.0
            )
            if prior_ret > -0.05:
                continue
            drift = abs(float(c[-1] / c[0] - 1))
            if drift > width * 0.60:
                continue
            score = (
                0.30 * _clip(floor_tests / 4)
                + 0.25 * _clip(ceiling_tests / 4)
                + 0.25 * _clip(-prior_ret / 0.25)
                + 0.20 * _clip((0.20 - width) / 0.16)
            )
            cand = _candidate(
                ticker,
                "P10",
                "Bearish rectangle",
                "short",
                score,
                trigger,
                ceiling + 0.25 * atr,
                trigger - (ceiling - trigger),
                start,
                end_bar,
                f"{w}d range; {floor_tests} support and {ceiling_tests} resistance tests",
                {"support": trigger, "resistance": ceiling},
                d,
                atr,
                initial_stop=round(trigger + atr, 4),
                target_basis="next major support preferred; rectangle-height measured move fallback",
                max_hold_bars=max(5, w // 2),
                exit_rule="Exit if the target is not reached within half the range duration",
                prior_trend_pct=round(prior_ret * 100, 2),
                volume_pattern="Prefer quiet range volume and expanding breakdown volume",
            )
            if best is None or cand.geometry_score > best.geometry_score:
                best = cand
    return best


def _swing_context(d: pd.DataFrame, atr: float, side: str):
    """Daily 20-EMA mean-reversion context shared by two-bar setups."""
    if len(d) < 30 or atr <= 0 or not _body_ok(d.iloc[-2]) or not _body_ok(d.iloc[-1]):
        return None
    pre = d["close"].iloc[-6:-1].to_numpy(float)
    changes = np.diff(pre)
    directional = int(np.sum(changes < 0 if side == "long" else changes > 0))
    if directional < 2:
        return None
    ema20 = float(ind.ema(d["close"], 20).iloc[-1])
    entry = float(d["close"].iloc[-1])
    distance = (ema20 - entry) / atr if side == "long" else (entry - ema20) / atr
    if distance < 0.50:
        return None
    return entry, ema20, distance


def _swing_candidate(
    ticker: str,
    d: pd.DataFrame,
    atr: float,
    *,
    code: str,
    name: str,
    side: str,
    score: float,
    detail: str,
    points: dict,
    stop: float,
    exit_rule: str | None = None,
):
    context = _swing_context(d, atr, side)
    if context is None:
        return None
    entry, ema20, distance = context
    return _candidate(
        ticker,
        code,
        name,
        side,
        score + 0.15 * _clip(distance / 3),
        entry,
        stop,
        ema20,
        len(d) - 2,
        len(d) - 1,
        detail,
        points,
        d,
        atr,
        setup_family="swing_reversion",
        entry_rule="Enter at the completed close of the second setup bar",
        stop_basis="daily close",
        initial_stop=round(stop, 4),
        conservative_target=round(ema20, 4),
        target_basis="20 EMA mean reversion",
        max_hold_bars=5,
        exit_rule=(
            exit_rule
            or "Exit by the sixth setup-count day (signal bar is day 1) or on an opposing reversal signal"
        ),
        volume_pattern="Volume is context only; no mandatory volume rule",
    )


def twizzler(ticker: str, d: pd.DataFrame, atr: float):
    """Near-equal two-bar low/high directed back toward the 20 EMA."""
    if len(d) < 30:
        return None
    a, b = d.iloc[-2], d.iloc[-1]
    candidates = []
    low_tol = max(0.001 * float((a["low"] + b["low"]) / 2), 0.12 * atr)
    if abs(float(a["low"] - b["low"])) <= low_tol:
        stop = min(float(a["low"]), float(b["low"])) - 0.10 * atr
        c = _swing_candidate(
            ticker,
            d,
            atr,
            code="S1",
            name="Twizzler bottom",
            side="long",
            score=0.62,
            detail=f"two-bar lows within {abs(float(a['low'] - b['low'])) / atr:.2f} ATR",
            points={"low_1": (len(d) - 2, a["low"]), "low_2": (len(d) - 1, b["low"])},
            stop=stop,
        )
        if c:
            candidates.append(c)
    high_tol = max(0.001 * float((a["high"] + b["high"]) / 2), 0.12 * atr)
    if abs(float(a["high"] - b["high"])) <= high_tol:
        stop = max(float(a["high"]), float(b["high"])) + 0.10 * atr
        c = _swing_candidate(
            ticker,
            d,
            atr,
            code="S2",
            name="Twizzler top",
            side="short",
            score=0.62,
            detail=f"two-bar highs within {abs(float(a['high'] - b['high'])) / atr:.2f} ATR",
            points={
                "high_1": (len(d) - 2, a["high"]),
                "high_2": (len(d) - 1, b["high"]),
            },
            stop=stop,
        )
        if c:
            candidates.append(c)
    return max(candidates, key=lambda x: x.geometry_score) if candidates else None


def engulfing(ticker: str, d: pd.DataFrame, atr: float):
    """Body engulfing reversal; wicks need not be engulfed."""
    if len(d) < 30:
        return None
    a, b = d.iloc[-2], d.iloc[-1]
    ao, ac, bo, bc = map(float, (a["open"], a["close"], b["open"], b["close"]))
    if ac < ao and bc > bo and bo <= ac and bc >= ao:
        return _swing_candidate(
            ticker,
            d,
            atr,
            code="S3",
            name="Bullish engulfing",
            side="long",
            score=0.78,
            detail="second real body fully engulfs the first; wicks ignored",
            points={"bar_1": (len(d) - 2, ac), "bar_2": (len(d) - 1, bc)},
            stop=min(float(a["low"]), float(b["low"])) - 0.10 * atr,
        )
    if ac > ao and bc < bo and bo >= ac and bc <= ao:
        return _swing_candidate(
            ticker,
            d,
            atr,
            code="S4",
            name="Bearish engulfing",
            side="short",
            score=0.78,
            detail="second real body fully engulfs the first; wicks ignored",
            points={"bar_1": (len(d) - 2, ac), "bar_2": (len(d) - 1, bc)},
            stop=max(float(a["high"]), float(b["high"])) + 0.10 * atr,
        )
    return None


def thrust_reversal(ticker: str, d: pd.DataFrame, atr: float):
    """Course-defined two-bar thrust reversal (kept distinct from textbook labels)."""
    if len(d) < 30:
        return None
    a, b = d.iloc[-2], d.iloc[-1]
    ao, ac, bo, bc = map(float, (a["open"], a["close"], b["open"], b["close"]))
    if ac < ao and bc > bo and bo > ac and bc > ao:
        return _swing_candidate(
            ticker,
            d,
            atr,
            code="S5",
            name="Bullish thrust reversal",
            side="long",
            score=0.70,
            detail="green bar opened above prior close and closed above prior open",
            points={"red_close": (len(d) - 2, ac), "green_close": (len(d) - 1, bc)},
            stop=min(float(a["low"]), float(b["low"])) - 0.10 * atr,
        )
    if ac > ao and bc < bo and bo < ac and bc < ao:
        return _swing_candidate(
            ticker,
            d,
            atr,
            code="S6",
            name="Bearish thrust reversal",
            side="short",
            score=0.70,
            detail="red bar opened below prior close and closed below prior open",
            points={"green_close": (len(d) - 2, ac), "red_close": (len(d) - 1, bc)},
            stop=max(float(a["high"]), float(b["high"])) + 0.10 * atr,
        )
    return None


def ema_left_out(ticker: str, d: pd.DataFrame, atr: float):
    """Three/five-EMA left-out-candle mean-reversion setup."""
    if len(d) < 30:
        return None
    a, b = d.iloc[-2], d.iloc[-1]
    ema3 = ind.ema(d["close"], 3)
    if float(a["high"]) < float(ema3.iloc[-2]) and float(b["close"]) > float(b["open"]):
        return _swing_candidate(
            ticker,
            d,
            atr,
            code="S7",
            name="Bullish EMA left-out",
            side="long",
            score=0.72,
            detail="prior candle—including wick—was below 3 EMA; signal candle closed green",
            points={
                "left_out": (len(d) - 2, a["low"]),
                "signal": (len(d) - 1, b["close"]),
            },
            stop=min(float(a["low"]), float(b["low"])) - 0.10 * atr,
            exit_rule="Exit after five sessions or on a daily close below the 5 EMA",
        )
    if float(a["low"]) > float(ema3.iloc[-2]) and float(b["close"]) < float(b["open"]):
        return _swing_candidate(
            ticker,
            d,
            atr,
            code="S8",
            name="Bearish EMA left-out",
            side="short",
            score=0.72,
            detail="prior candle—including wick—was above 3 EMA; signal candle closed red",
            points={
                "left_out": (len(d) - 2, a["high"]),
                "signal": (len(d) - 1, b["close"]),
            },
            stop=max(float(a["high"]), float(b["high"])) + 0.10 * atr,
            exit_rule="Exit after five sessions or on a daily close above the 5 EMA",
        )
    return None


DETECTORS: tuple[Callable, ...] = (
    flat_base,
    bearish_rectangle,
    cup_handle,
    inverse_head_shoulders,
    double_bottom,
    ascending_triangle,
    head_shoulders_top,
    double_top,
    flag,
    twizzler,
    engulfing,
    thrust_reversal,
    ema_left_out,
)


def detect_all(ticker: str, daily: pd.DataFrame) -> list[PatternCandidate]:
    """Run every detector and de-duplicate same-side patterns sharing a trigger."""
    if daily is None or len(daily) < 90:
        return []
    d = daily.dropna(subset=["open", "high", "low", "close", "volume"]).copy()
    atr = _atr(d)
    if len(d) < 90 or atr <= 0:
        return []
    found = []
    completion_limits = {
        "P1": 5,
        "P2": 20,
        "P3": 35,
        "P4": 35,
        "P5": 15,
        "P6": 35,
        "P7": 35,
        "P8": 5,
        "P9": 5,
        "P10": 5,
        **{f"S{i}": 0 for i in range(1, 9)},
    }
    for detector in DETECTORS:
        try:
            c = detector(ticker, d, atr)
            if (
                c is not None
                and c.status not in {"FAILED", "INVALID", "EXPIRED"}
                and (c.bars_since_completion or 0) <= completion_limits.get(c.code, 20)
            ):
                found.append(c)
        except Exception:
            continue
    found.sort(key=lambda c: c.geometry_score, reverse=True)
    out = []
    for c in found:
        duplicate = any(
            c.setup_family == x.setup_family
            and c.side == x.side
            and abs(c.trigger - x.trigger) <= 0.35 * atr
            and abs(c.start_bar - x.start_bar) <= 10
            for x in out
        )
        if not duplicate:
            out.append(c)
    return out
