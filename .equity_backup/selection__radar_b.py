"""Radar-B — base-formation screener for the equity LEAPS engine.

Extends Stock Opportunity Radar rather than paralleling it: same universe,
same bar format, same MIN_DOLLAR_VOLUME floor.

ARCHITECTURAL POINT: Radar-B membership is SILENT. It produces a watchlist,
not a signal. A separate trigger (see `trigger`) fires entries.

Stage 1 bases fail to resolve more often than they resolve, and the failures
look identical to the successes right up until they don't. Collapsing the
watchlist into the signal produces five confident-looking bases every Saturday
and a machine for manufacturing conviction that has not been earned.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from core.fundamentals import Fundamentals, fundamental_gates

POLICY_ID = "radar-b-v1"

# Structural gates — docs/equity_leaps.md §1.2
MIN_DRAWDOWN = 0.25
MAX_DRAWDOWN = 0.60
MIN_BASE_WEEKS = 6
NO_NEW_LOW_SESSIONS = 15
MAX_ATR_COMPRESSION = 0.80
MAX_BASE_WIDTH = 0.25
MIN_DOLLAR_VOLUME = 50_000_000.0
MIN_BARS = 260

OUTPUT_LIMIT = 5

# Trigger — §1.4
MIN_TRIGGER_VOL_MULT = 1.5
EARNINGS_BLACKOUT_SESSIONS = 5


# --------------------------------------------------------------- helpers --
def _closes(bars): return [b["close"] for b in bars]


def _sma(xs: list[float], n: int) -> float | None:
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _atr(bars: list[dict], n: int) -> float | None:
    if len(bars) < n + 1:
        return None
    trs = []
    for prev, cur in zip(bars[-n - 1:-1], bars[-n:]):
        trs.append(max(cur["high"] - cur["low"],
                       abs(cur["high"] - prev["close"]),
                       abs(cur["low"] - prev["close"])))
    return sum(trs) / n if trs else None


def _higher_lows(bars: list[dict], low_idx: int, window: int = 10) -> int:
    """Count non-overlapping swing lows above the base low, after the low."""
    tail = bars[low_idx:]
    if len(tail) < window * 2:
        return 0
    lows, i = [], window
    while i < len(tail) - window:
        seg = tail[i - window:i + window]
        if tail[i]["low"] == min(b["low"] for b in seg):
            lows.append(tail[i]["low"])
            i += window
        else:
            i += 1
    return sum(1 for a, b in zip(lows, lows[1:]) if b > a)


def _pctl(value: float, pool: list[float]) -> float:
    if not pool:
        return 0.5
    return sum(1 for x in pool if x <= value) / len(pool)


# --------------------------------------------------------------- metrics --
@dataclass
class BaseMetrics:
    symbol: str
    price: float = 0.0
    high_52w: float = 0.0
    low_52w: float = 0.0
    drawdown: float = 0.0
    base_weeks: float = 0.0
    sessions_since_new_low: int = 0
    atr_compression: float = 0.0
    base_width: float = 0.0
    dollar_volume: float = 0.0
    volume_dryup: float = 1.0
    higher_lows: int = 0
    rs_slope: float = 0.0
    sma50: float | None = None
    sma50_slope: float = 0.0
    ok: bool = False
    blocks: list[str] = field(default_factory=list)


def base_metrics(symbol: str, bars: list[dict],
                 bench_bars: list[dict] | None = None) -> BaseMetrics:
    m = BaseMetrics(symbol=symbol)
    if len(bars) < MIN_BARS:
        m.blocks.append(
            f"Only {len(bars)} daily bars are available for {symbol}, below the "
            f"{MIN_BARS} required to measure a 52-week base, so the name cannot "
            f"be assessed.")
        return m

    window = bars[-252:]
    closes = _closes(bars)
    m.price = closes[-1]
    m.high_52w = max(b["high"] for b in window)
    m.low_52w = min(b["low"] for b in window)
    m.drawdown = (m.high_52w - m.price) / m.high_52w if m.high_52w else 0.0

    low_idx = min(range(len(window)), key=lambda i: window[i]["low"])
    m.base_weeks = (len(window) - 1 - low_idx) / 5.0

    lows = [b["low"] for b in bars]
    m.sessions_since_new_low = 0
    for i in range(len(bars) - 1, max(len(bars) - 60, 20), -1):
        if lows[i] <= min(lows[i - 20:i]):
            break
        m.sessions_since_new_low += 1

    atr20, atr100 = _atr(bars, 20), _atr(bars, 100)
    m.atr_compression = (atr20 / atr100) if atr20 and atr100 else 1.0

    last8w = bars[-40:]
    hi, lo = max(b["high"] for b in last8w), min(b["low"] for b in last8w)
    m.base_width = (hi - lo) / m.price if m.price else 1.0

    vols = [b.get("volume", 0.0) for b in bars]
    m.dollar_volume = (sum(vols[-90:]) / 90.0) * m.price if len(vols) >= 90 else 0.0
    v20, v100 = sum(vols[-20:]) / 20.0, sum(vols[-100:]) / 100.0
    m.volume_dryup = (v20 / v100) if v100 else 1.0

    m.higher_lows = _higher_lows(window, low_idx)

    m.sma50 = _sma(closes, 50)
    prev50 = _sma(closes[:-10], 50)
    if m.sma50 and prev50:
        m.sma50_slope = (m.sma50 - prev50) / prev50

    if bench_bars and len(bench_bars) >= 21:
        bc = _closes(bench_bars)
        rs_now = m.price / bc[-1]
        rs_then = closes[-21] / bc[-21]
        m.rs_slope = (rs_now - rs_then) / rs_then if rs_then else 0.0

    m.ok = True
    return m


# ----------------------------------------------------------------- gates --
def structural_gates(m: BaseMetrics) -> tuple[bool, list[str]]:
    """Return (passed, full-sentence reasons)."""
    if not m.ok:
        return False, m.blocks

    reasons, passed = [], True

    if not (MIN_DRAWDOWN <= m.drawdown <= MAX_DRAWDOWN):
        passed = False
        reasons.append(
            f"The drawdown from the 52-week high is {m.drawdown * 100:.0f}%, outside "
            f"the {MIN_DRAWDOWN * 100:.0f}-{MAX_DRAWDOWN * 100:.0f}% band, so this is "
            f"either not a real reset or a decline deep enough that the failure "
            f"rate climbs sharply.")
    else:
        reasons.append(
            f"The drawdown from the 52-week high is {m.drawdown * 100:.0f}%, deep "
            f"enough to count as a genuine reset without entering the range where "
            f"recovery rates deteriorate.")

    if m.base_weeks < MIN_BASE_WEEKS:
        passed = False
        reasons.append(
            f"Only {m.base_weeks:.0f} weeks have passed since the 52-week low, "
            f"under the {MIN_BASE_WEEKS}-week minimum, so the name is still falling "
            f"rather than basing.")

    if m.sessions_since_new_low < NO_NEW_LOW_SESSIONS:
        passed = False
        reasons.append(
            f"A new 20-day low was set {m.sessions_since_new_low} sessions ago, "
            f"inside the {NO_NEW_LOW_SESSIONS}-session requirement, which is the "
            f"cheapest available proof that the decline has not yet stopped.")

    if m.atr_compression >= MAX_ATR_COMPRESSION:
        passed = False
        reasons.append(
            f"The 20-day ATR is {m.atr_compression:.2f}x the 100-day ATR, above the "
            f"{MAX_ATR_COMPRESSION} ceiling, so volatility has not contracted and the "
            f"name is not actually consolidating.")
    else:
        reasons.append(
            f"The 20-day ATR has contracted to {m.atr_compression:.2f}x the 100-day "
            f"ATR, which is what consolidation looks like quantitatively.")

    if m.base_width > MAX_BASE_WIDTH:
        passed = False
        reasons.append(
            f"The eight-week range spans {m.base_width * 100:.0f}% of price, wider than "
            f"the {MAX_BASE_WIDTH * 100:.0f}% ceiling, which makes this a slower "
            f"downtrend rather than a base.")

    if m.dollar_volume < MIN_DOLLAR_VOLUME:
        passed = False
        reasons.append(
            f"Average daily dollar volume is ${m.dollar_volume / 1e6:.0f}M, below the "
            f"${MIN_DOLLAR_VOLUME / 1e6:.0f}M floor, and thin long-dated strikes give "
            f"back more at the fills than the structure earns.")

    return passed, reasons


# ----------------------------------------------------------------- score --
def quality_score(m: BaseMetrics, f: Fundamentals,
                  pool: dict[str, list[float]] | None = None) -> tuple[float, dict]:
    """0-100 quality score. Ranking only — never a gate."""
    pool = pool or {}
    parts = {}

    parts["base_tightness"] = 25.0 * (1.0 - _pctl(m.atr_compression,
                                                  pool.get("atr_compression", [])))
    trend = f.eps_trend_90d if f.eps_trend_90d is not None else 0.0
    parts["estimate_revisions"] = 25.0 * max(0.0, min(1.0, 0.5 + trend * 5))
    parts["rs_inflection"] = 20.0 * max(0.0, min(1.0, 0.5 + m.rs_slope * 10))
    parts["volume_dryup"] = 15.0 * max(0.0, min(1.0, 1.0 - m.volume_dryup))
    parts["base_structure"] = 15.0 * min(1.0, m.higher_lows / 3.0)

    total = round(sum(parts.values()), 1)
    return total, {k: round(v, 1) for k, v in parts.items()}


# --------------------------------------------------------------- scanner --
def scan(candidates: dict[str, list[dict]], fundamentals: dict[str, Fundamentals],
         bench_bars: list[dict] | None = None,
         limit: int = OUTPUT_LIMIT) -> dict:
    """Run Radar-B. Returns watchlist payload — NOT signals.

    Returns fewer than `limit` when fewer qualify. A screener that always
    returns exactly five has stopped screening.
    """
    metrics = {s: base_metrics(s, bars, bench_bars)
               for s, bars in candidates.items()}
    pool = {"atr_compression": [m.atr_compression for m in metrics.values() if m.ok]}

    survivors, rejected = [], []
    for symbol, m in metrics.items():
        struct_ok, struct_reasons = structural_gates(m)
        f = fundamentals.get(symbol) or Fundamentals(symbol=symbol)
        fund_ok, fund_reasons = fundamental_gates(f)

        row = {"symbol": symbol, "metrics": m, "fundamentals": f,
               "reasons": struct_reasons + fund_reasons,
               "unrated": f.status != "OK"}

        if struct_ok and fund_ok:
            score, parts = quality_score(m, f, pool)
            row.update(score=score, score_parts=parts)
            survivors.append(row)
        else:
            row["score"] = 0.0
            rejected.append(row)

    survivors.sort(key=lambda r: r["score"], reverse=True)
    return {"policy_id": POLICY_ID,
            "watchlist": survivors[:limit],
            "qualified": len(survivors),
            "rejected": len(rejected),
            "returned": min(len(survivors), limit),
            "note": ("Radar-B membership is a watchlist, not a signal. Entries "
                     "fire on the separate trigger only.")}


# --------------------------------------------------------------- trigger --
def trigger(m: BaseMetrics, bars: list[dict],
            earnings: date | None = None,
            today: date | None = None) -> dict:
    """The reclaim event. Separate from watchlist membership by design."""
    today = today or date.today()
    checks, fired = [], True

    above = m.sma50 is not None and m.price > m.sma50
    if above:
        checks.append(
            f"Price at {m.price:.2f} has closed back above the 50-day at "
            f"{m.sma50:.2f}, which is the reclaim rather than merely a touch.")
    else:
        fired = False
        checks.append(
            f"Price at {m.price:.2f} has not closed above the 50-day at "
            f"{(m.sma50 or 0):.2f}, so the level has been tested but not reclaimed.")

    if m.sma50_slope >= 0:
        checks.append(
            f"The 50-day slope is {m.sma50_slope * 100:+.1f}% over ten sessions, so the "
            f"line is flat to rising and can act as support.")
    else:
        fired = False
        checks.append(
            f"The 50-day slope is {m.sma50_slope * 100:+.1f}% over ten sessions, so the "
            f"line is still falling and offers no support to reclaim.")

    vols = [b.get("volume", 0.0) for b in bars]
    mult = (vols[-1] / (sum(vols[-21:-1]) / 20.0)) if len(vols) >= 21 and sum(vols[-21:-1]) else 0.0
    if mult >= MIN_TRIGGER_VOL_MULT:
        checks.append(
            f"Reclaim-day volume ran {mult:.1f}x the twenty-day average, confirming "
            f"the move came with participation rather than drift.")
    else:
        fired = False
        checks.append(
            f"Reclaim-day volume ran {mult:.1f}x the twenty-day average, below the "
            f"{MIN_TRIGGER_VOL_MULT}x requirement, so the move lacks participation.")

    if earnings and 0 <= (earnings - today).days <= EARNINGS_BLACKOUT_SESSIONS:
        fired = False
        checks.append(
            f"Earnings land in {(earnings - today).days} days, inside the "
            f"{EARNINGS_BLACKOUT_SESSIONS}-session blackout, so entry is deferred "
            f"until the event has passed.")

    return {"fired": fired, "checks": checks,
            "level": round(m.sma50, 2) if m.sma50 else None}
