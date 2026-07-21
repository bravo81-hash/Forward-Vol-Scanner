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
    volume_confirmed: bool = False
    detail: str = ""
    points: dict = field(default_factory=dict)

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
            return value
        return native(asdict(self))


def _clip(x: float) -> float:
    return float(max(0.0, min(1.0, x)))


def _atr(d: pd.DataFrame) -> float:
    x = float(ind.atr(d).iloc[-1])
    return x if np.isfinite(x) and x > 0 else 0.0


def _pivots(d: pd.DataFrame, left: int = 3, right: int = 3):
    """Confirmed integer-index pivots; the final ``right`` bars are excluded."""
    h, l = d["high"].to_numpy(float), d["low"].to_numpy(float)
    ph, pl = [], []
    for i in range(left, len(d) - right):
        wh, wl = h[i - left:i + right + 1], l[i - left:i + right + 1]
        if h[i] == np.max(wh) and int(np.argmax(wh)) == left:
            ph.append((i, float(h[i])))
        if l[i] == np.min(wl) and int(np.argmin(wl)) == left:
            pl.append((i, float(l[i])))
    return ph, pl


def _between(points: Iterable[tuple[int, float]], lo: int, hi: int):
    return [(i, p) for i, p in points if lo < i < hi]


def _recent_cross_age(close: np.ndarray, trigger: float, side: str,
                      penetration: float, start_bar: int = 0) -> int | None:
    """Age of the latest material break formed during this pattern.

    Earlier history often traded beyond today's trigger (notably before a cup,
    double bottom, or reversal).  Counting those bars would mislabel an
    unfinished setup as a retest, so breakout history starts at the candidate's
    first pattern bar.
    """
    start = max(0, min(int(start_bar), len(close) - 1))
    window = close[start:]
    if side == "long":
        hits = np.where(window > trigger + penetration)[0]
    else:
        hits = np.where(window < trigger - penetration)[0]
    return int(len(window) - 1 - hits[-1]) if len(hits) else None


def classify(candidate: PatternCandidate, d: pd.DataFrame, atr: float,
             near_atr: float = 1.0, penetration_atr: float = 0.25,
             retest_atr: float = 0.35) -> PatternCandidate:
    """Attach a mutually-exclusive setup state using daily closes only.

    ``TRIGGERED_INTRADAY`` is reserved for the TWS live overlay.  A one-cent
    breach is never a close confirmation: the default penetration is 0.25 ATR.
    """
    if atr <= 0:
        return candidate
    close = d["close"].to_numpy(float)
    last = float(close[-1])
    vol = d["volume"].to_numpy(float)
    vol_avg = float(np.nanmean(vol[-21:-1])) if len(vol) > 21 else float(np.nanmean(vol))
    candidate.volume_confirmed = bool(vol_avg > 0 and vol[-1] >= 1.2 * vol_avg)
    pen, ret = penetration_atr * atr, retest_atr * atr
    age = _recent_cross_age(
        close, candidate.trigger, candidate.side, pen, candidate.start_bar)
    candidate.breakout_age = age

    if candidate.side == "long":
        candidate.distance_atr = round((candidate.trigger - last) / atr, 2)
        if last < candidate.invalidation:
            status = "FAILED"
        elif age is not None and 0 < age <= 10 and last >= candidate.trigger - ret and last <= candidate.trigger + ret:
            status = "RETESTING"
        elif last > candidate.trigger + pen:
            status = "CLOSE_CONFIRMED"
        elif abs(candidate.trigger - last) <= near_atr * atr:
            status = "NEAR_TRIGGER"
        else:
            status = "FORMING"
    else:
        candidate.distance_atr = round((last - candidate.trigger) / atr, 2)
        if last > candidate.invalidation:
            status = "FAILED"
        elif age is not None and 0 < age <= 10 and last <= candidate.trigger + ret and last >= candidate.trigger - ret:
            status = "RETESTING"
        elif last < candidate.trigger - pen:
            status = "CLOSE_CONFIRMED"
        elif abs(last - candidate.trigger) <= near_atr * atr:
            status = "NEAR_TRIGGER"
        else:
            status = "FORMING"
    candidate.status = status
    return candidate


def _candidate(ticker, code, name, side, score, trigger, invalidation,
               target, start, end, detail, points, d, atr):
    c = PatternCandidate(ticker, code, name, side, round(_clip(score), 3),
                         round(float(trigger), 4), round(float(invalidation), 4),
                         round(float(target), 4), int(start), int(end),
                         detail=detail, points=points)
    return classify(c, d, atr)


def flat_base(ticker: str, d: pd.DataFrame, atr: float):
    """Flat base / VCP: prior advance, repeated ceiling and contracting range."""
    if len(d) < 90 or atr <= 0:
        return None
    best = None
    for w in (25, 35, 50, 65):
        x = d.tail(w)
        h, l, c = x["high"].to_numpy(float), x["low"].to_numpy(float), x["close"].to_numpy(float)
        trigger = float(np.quantile(h, 0.90))
        floor = float(np.quantile(l, 0.10))
        mid = float(np.mean(c))
        width = (trigger - floor) / mid if mid else 1.0
        if not (0.035 <= width <= 0.22):
            continue
        tol = max(0.012 * trigger, 0.45 * atr)
        touches = int(np.sum(np.abs(h - trigger) <= tol))
        if touches < 2:
            continue
        half = max(7, w // 2)
        r1 = float(np.max(h[:half]) - np.min(l[:half]))
        r2 = float(np.max(h[-half:]) - np.min(l[-half:]))
        contract = 1 - r2 / r1 if r1 > 0 else 0
        prior = d.iloc[-w - 60:-w]["close"]
        prior_ret = float(prior.iloc[-1] / prior.iloc[0] - 1) if len(prior) > 20 else 0.0
        if prior_ret < 0.05 or contract < 0.05:
            continue
        lows = np.minimum.accumulate(l[::-1])[::-1]
        compression = _clip(contract / 0.50)
        score = 0.30 * _clip(touches / 4) + 0.30 * compression + 0.20 * _clip(prior_ret / 0.25) + 0.20 * _clip((0.22 - width) / 0.18)
        start = len(d) - w
        cand = _candidate(ticker, "P1", "Flat base / VCP", "long", score,
                          trigger, floor - 0.25 * atr, trigger + (trigger - floor),
                          start, len(d) - 1,
                          f"{w}d base; {touches} resistance tests; range contracted {contract:.0%}",
                          {"resistance": round(trigger, 2), "support": round(floor, 2)}, d, atr)
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
        left = int(np.argmax(c[:max(20, n // 3)]))
        trough = (left + 5 + int(np.argmin(c[left + 5:int(n * 0.78)]))
                  if int(n * 0.78) > left + 5 else left)
        if trough <= left + 5:
            continue
        right_start = trough + 8
        if right_start >= n - 8:
            continue
        right = right_start + int(np.argmax(c[right_start:n - 5]))
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
        handle_high = float(handle["high"].max())
        handle_depth = (rim - handle_low) / rim
        if handle_depth < -0.03 or handle_depth > min(0.18, depth * 0.55):
            continue
        # A rounded bottom spends time near the low; a one-bar V does not.
        near_bottom = int(np.sum(c <= bottom + 0.25 * (rim - bottom)))
        if near_bottom < max(4, int(0.08 * w)):
            continue
        prior = d.iloc[-w - 50:-w]["close"]
        prior_ret = float(prior.iloc[-1] / prior.iloc[0] - 1) if len(prior) > 20 else 0.0
        if prior_ret < 0:
            continue
        trigger = max(rim1, rim2, handle_high)
        score = (0.28 * _clip(1 - symmetry / 0.10)
                 + 0.25 * _clip(near_bottom / (0.18 * w))
                 + 0.25 * _clip(1 - handle_depth / 0.18)
                 + 0.22 * _clip(prior_ret / 0.25))
        start = len(d) - w + left
        cand = _candidate(ticker, "P2", "Cup and handle", "long", score,
                          trigger, handle_low - 0.25 * atr,
                          trigger + (trigger - bottom), start, len(d) - 1,
                          f"cup depth {depth:.0%}; handle {handle_depth:.0%}; rim gap {symmetry:.1%}",
                          {"left_rim": round(rim1, 2), "bottom": round(bottom, 2),
                           "right_rim": round(rim2, 2), "handle_low": round(handle_low, 2)}, d, atr)
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
        s1, s2, s3 = swings[a:a + 3]
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
        left_neck = _between(opposite, s1[0], s2[0])
        right_neck = _between(opposite, s2[0], s3[0])
        if not left_neck or not right_neck:
            continue
        p1 = max(left_neck, key=lambda z: z[1]) if bullish else min(left_neck, key=lambda z: z[1])
        p2 = max(right_neck, key=lambda z: z[1]) if bullish else min(right_neck, key=lambda z: z[1])
        slope = (p2[1] - p1[1]) / max(1, p2[0] - p1[0])
        trigger = p2[1] + slope * max(0, (len(d.tail(180)) - 1 - p2[0]))
        height = abs(trigger - s2[1])
        if height < 2 * atr:
            continue
        symmetry = 1 - abs((s2[0] - s1[0]) - (s3[0] - s2[0])) / max(s3[0] - s1[0], 1)
        score = 0.35 * _clip(head_prom / 0.15) + 0.30 * _clip(1 - shoulder_gap / 0.08) + 0.35 * _clip(symmetry)
        if bullish:
            cand = _candidate(ticker, "P3", "Inverse head and shoulders", "long", score,
                              trigger, min(s1[1], s3[1]) - 0.25 * atr,
                              trigger + height, off + s1[0], len(d) - 1,
                              f"head {head_prom:.1%} below shoulders; neckline slope {slope:.3f}/bar",
                              {"left_shoulder": round(s1[1], 2), "head": round(s2[1], 2),
                               "right_shoulder": round(s3[1], 2)}, d, atr)
        else:
            cand = _candidate(ticker, "P6", "Head and shoulders top", "short", score,
                              trigger, max(s1[1], s3[1]) + 0.25 * atr,
                              trigger - height, off + s1[0], len(d) - 1,
                              f"head {head_prom:.1%} above shoulders; neckline slope {slope:.3f}/bar",
                              {"left_shoulder": round(s1[1], 2), "head": round(s2[1], 2),
                               "right_shoulder": round(s3[1], 2)}, d, atr)
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
        mid = max(mids, key=lambda z: z[1]) if bullish else min(mids, key=lambda z: z[1])
        base = (s1[1] + s2[1]) / 2
        depth = abs(mid[1] - base) / base
        if depth < 0.055 or abs(mid[1] - base) < 2 * atr:
            continue
        # Broad/rounded pivots score above isolated single-bar spikes.
        center = off + s2[0]
        local = d.iloc[max(0, center - 3):min(len(d), center + 4)]
        if bullish:
            broad = int(np.sum(local["low"] <= s2[1] + 0.5 * atr))
            invalid = min(s1[1], s2[1]) - 0.25 * atr
            target = mid[1] + (mid[1] - base)
            code, name, side = "P4", "Double bottom", "long"
        else:
            broad = int(np.sum(local["high"] >= s2[1] - 0.5 * atr))
            invalid = max(s1[1], s2[1]) + 0.25 * atr
            target = mid[1] - (base - mid[1])
            code, name, side = "P7", "Double top", "short"
        score = 0.38 * _clip(1 - similarity / 0.055) + 0.32 * _clip(depth / 0.18) + 0.30 * _clip(broad / 4)
        cand = _candidate(ticker, code, name, side, score, mid[1], invalid,
                          target, off + s1[0], len(d) - 1,
                          f"peaks/troughs {similarity:.1%} apart; {sep} bars; depth {depth:.0%}",
                          {"first": round(s1[1], 2), "middle": round(mid[1], 2),
                           "second": round(s2[1], 2)}, d, atr)
        if best is None or cand.geometry_score > best.geometry_score:
            best = cand
    return best


def double_bottom(ticker, d, atr):
    return _double(ticker, d, atr, True)


def double_top(ticker, d, atr):
    return _double(ticker, d, atr, False)


def ascending_triangle(ticker, d, atr):
    if len(d) < 100 or atr <= 0:
        return None
    best = None
    for w in (35, 50, 70, 90):
        x = d.tail(w)
        ph, pl = _pivots(x, 2, 2)
        if len(ph) < 2 or len(pl) < 2:
            continue
        resistance = float(np.median([p for _, p in ph[-4:]]))
        tol = max(0.015 * resistance, 0.55 * atr)
        tests = [(i, p) for i, p in ph if abs(p - resistance) <= tol]
        if len(tests) < 2:
            continue
        lows = [(i, p) for i, p in pl if i >= tests[0][0] - 5]
        if len(lows) < 2 or lows[-1][1] <= lows[-2][1]:
            continue
        rise = (lows[-1][1] - lows[0][1]) / max(lows[0][1], 1e-9)
        height = resistance - min(p for _, p in lows)
        if rise < 0.02 or height < 2 * atr:
            continue
        dispersion = np.std([p for _, p in tests]) / resistance
        score = 0.35 * _clip(len(tests) / 4) + 0.35 * _clip(rise / 0.12) + 0.30 * _clip(1 - dispersion / 0.02)
        off = len(d) - w
        cand = _candidate(ticker, "P5", "Ascending triangle", "long", score,
                          resistance, lows[-1][1] - 0.25 * atr,
                          resistance + height, off + min(tests[0][0], lows[0][0]), len(d) - 1,
                          f"{len(tests)} ceiling tests; lows rose {rise:.1%}",
                          {"resistance": round(resistance, 2), "last_low": round(lows[-1][1], 2)}, d, atr)
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
            fx = d.iloc[end:end + flag_n]
            flag_move = float(fx["close"].iloc[-1] - fx["close"].iloc[0])
            retrace = abs(flag_move / pole) if pole else 1.0
            if retrace > 0.52 or (side == "long" and flag_move > 0.35 * atr) or (side == "short" and flag_move < -0.35 * atr):
                continue
            flag_vol = float(np.mean(v[end:]))
            pole_vol = float(np.mean(v[start:end]))
            vol_contract = 1 - flag_vol / pole_vol if pole_vol > 0 else 0
            if vol_contract < -0.10:
                continue
            if side == "long":
                trigger, invalid = float(fx["high"].max()), float(fx["low"].min()) - 0.25 * atr
                target = trigger + abs(pole)
                code, name = "P8", "Bull flag"
            else:
                trigger, invalid = float(fx["low"].min()), float(fx["high"].max()) + 0.25 * atr
                target = trigger - abs(pole)
                code, name = "P9", "Bear flag"
            score = 0.35 * _clip((pole_atr - 2.5) / 4) + 0.35 * _clip(1 - retrace / 0.52) + 0.30 * _clip((vol_contract + 0.1) / 0.5)
            cand = _candidate(ticker, code, name, side, score, trigger, invalid,
                              target, start, len(d) - 1,
                              f"{pole_n}d pole {pole_pct:.0%}; {flag_n}d flag; retrace {retrace:.0%}; volume {vol_contract:.0%} lower",
                              {"pole_start": round(c[start], 2), "pole_end": round(c[end - 1], 2)}, d, atr)
            if best is None or cand.geometry_score > best.geometry_score:
                best = cand
    return best


DETECTORS: tuple[Callable, ...] = (
    flat_base,
    cup_handle,
    inverse_head_shoulders,
    double_bottom,
    ascending_triangle,
    head_shoulders_top,
    double_top,
    flag,
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
    for detector in DETECTORS:
        try:
            c = detector(ticker, d, atr)
            if c is not None and c.status != "FAILED":
                found.append(c)
        except Exception:
            continue
    found.sort(key=lambda c: c.geometry_score, reverse=True)
    out = []
    for c in found:
        duplicate = any(c.side == x.side and abs(c.trigger - x.trigger) <= 0.35 * atr
                        and abs(c.start_bar - x.start_bar) <= 10 for x in out)
        if not duplicate:
            out.append(c)
    return out
