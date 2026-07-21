"""Indicator primitives. Pure functions over OHLC DataFrames (lowercase cols)."""
import numpy as np
import pandas as pd


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int | None = None) -> pd.Series:
    n = n or 14
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(span=n, adjust=False).mean()


def donchian(df: pd.DataFrame, n: int):
    """Prior-N high/low (shifted so the current bar can break it)."""
    hi = df["high"].rolling(n).max().shift(1)
    lo = df["low"].rolling(n).min().shift(1)
    return hi, lo


def pivots(df: pd.DataFrame, left: int, right: int):
    """Confirmed fractal pivots. Returns (highs, lows) as lists of (ts, price).
    A bar is a pivot high if its high is the unique max over [i-left, i+right];
    the last `right` bars cannot yet be pivots (no look-ahead)."""
    highs, lows = [], []
    H, L = df["high"].to_numpy(), df["low"].to_numpy()
    idx = df.index
    n = len(df)
    for i in range(left, n - right):
        wH = H[i - left:i + right + 1]
        wL = L[i - left:i + right + 1]
        if H[i] == wH.max() and int(wH.argmax()) == left:
            highs.append((idx[i], float(H[i])))
        if L[i] == wL.min() and int(wL.argmin()) == left:
            lows.append((idx[i], float(L[i])))
    return highs, lows


def cluster(levels, tol: float):
    """Merge nearby price levels into zones. Returns [(price, count)] sorted by price."""
    if not levels:
        return []
    xs = sorted(float(x) for x in levels)
    zones, grp = [], [xs[0]]
    for x in xs[1:]:
        if abs(x - float(np.mean(grp))) <= tol:
            grp.append(x)
        else:
            zones.append((float(np.mean(grp)), len(grp)))
            grp = [x]
    zones.append((float(np.mean(grp)), len(grp)))
    return zones


def adx(df: pd.DataFrame, n: int | None = None):
    """Wilder ADX with +DI/-DI. Returns (adx, plus_di, minus_di) Series."""
    n = n or 14
    h, l, c = df["high"], df["low"], df["close"]
    up = h.diff()
    dn = -l.diff()
    plus_dm = up.where((up > dn) & (up > 0), 0.0)
    minus_dm = dn.where((dn > up) & (dn > 0), 0.0)
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    a = 1.0 / n  # Wilder smoothing
    atr_n = tr.ewm(alpha=a, adjust=False).mean()
    plus_di = 100 * plus_dm.ewm(alpha=a, adjust=False).mean() / atr_n
    minus_di = 100 * minus_dm.ewm(alpha=a, adjust=False).mean() / atr_n
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=a, adjust=False).mean(), plus_di, minus_di


def realized_vol(df: pd.DataFrame, window: int | None = None) -> pd.Series:
    """Annualised close-to-close realized volatility."""
    window = window or 20
    r = np.log(df["close"] / df["close"].shift(1))
    return r.rolling(window).std() * np.sqrt(252)


def pct_rank(series: pd.Series, lookback: int) -> float:
    """Percentile (0..100) of the last value within its trailing `lookback` window."""
    s = series.dropna().tail(lookback)
    if len(s) < 2:
        return float("nan")
    last = s.iloc[-1]
    return float((s < last).mean() * 100)


def rsi(closes, n=3):
    """Wilder RSI, causal (ewm alpha=1/n from bar 0). NaN-safe -> 50 during warmup."""
    delta = closes.diff().fillna(0.0)
    up = delta.clip(lower=0.0)
    dn = (-delta).clip(lower=0.0)
    au = up.ewm(alpha=1.0 / n, adjust=False).mean()
    ad = dn.ewm(alpha=1.0 / n, adjust=False).mean()
    out = 100.0 - 100.0 / (1.0 + au / ad)
    out = out.where(ad > 0, 100.0)          # pure-up tape
    out = out.where((au > 0) | (ad > 0), 50.0)
    return out.fillna(50.0)
