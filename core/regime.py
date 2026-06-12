"""TE Console composites, ported — single source of truth with the Pine script.

Inputs: daily OHLC bars + an IV30 history series (live: TWS historical
OPTION_IMPLIED_VOLATILITY, one cached request; mock: synthetic).
"""
from __future__ import annotations
import math
import random
import zlib
from datetime import date, timedelta

ADX_THR = 20
IV_SPIKE_PCT = 12.0


# ----------------------------------------------------------- bar statistics --
def _ema(xs, n):
    k, e = 2 / (n + 1), xs[0]
    for x in xs[1:]:
        e = x * k + e * (1 - k)
    return e


def _stdev(xs):
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _rv(closes, n):
    rs = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - n, len(closes))]
    return _stdev(rs) * math.sqrt(252) * 100


def _adx(highs, lows, closes, n=14):
    trs, pdms, ndms = [], [], []
    for i in range(1, len(closes)):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]),
                       abs(lows[i] - closes[i - 1])))
        up, dn = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdms.append(up if up > dn and up > 0 else 0.0)
        ndms.append(dn if dn > up and dn > 0 else 0.0)
    if len(trs) < 2 * n:
        return 0.0
    atr, pds, nds = sum(trs[:n]), sum(pdms[:n]), sum(ndms[:n])
    dxs = []
    for i in range(n, len(trs)):
        atr = atr - atr / n + trs[i]
        pds = pds - pds / n + pdms[i]
        nds = nds - nds / n + ndms[i]
        pdi, ndi = 100 * pds / atr, 100 * nds / atr
        dxs.append(100 * abs(pdi - ndi) / max(pdi + ndi, 1e-9))
    return sum(dxs[-n:]) / n


def _autocorr(closes, n=20):
    rs = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))][-n - 1:]
    a, b = rs[1:], rs[:-1]
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    cov = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return cov / den if den else 0.0


def _parkinson(highs, lows, n=10):
    hl = [math.log(h / l) ** 2 for h, l in zip(highs[-n:], lows[-n:])]
    return math.sqrt(sum(hl) / n / (4 * math.log(2))) * math.sqrt(252) * 100


# ------------------------------------------------------------------ regime --
def compute_regime(bars, iv30_hist, iv30_now: float) -> dict:
    """bars: [(date,o,h,l,c)] oldest->newest; iv30_hist: [iv%] daily, 1y."""
    cs = [b[4] for b in bars]
    hs = [b[2] for b in bars]
    ls = [b[3] for b in bars]
    e8, e21, e50 = _ema(cs[-60:], 8), _ema(cs[-90:], 21), _ema(cs[-150:], 50)
    adx = _adx(hs, ls, cs)
    rv7, rv21 = _rv(cs, 7), _rv(cs, 21)
    rvcc10, park10 = _rv(cs, 10), _parkinson(hs, ls, 10)
    ac20 = _autocorr(cs, 20)
    atr = sum(max(hs[i] - ls[i], abs(hs[i] - cs[i - 1])) for i in range(-14, 0)) / 14
    spot = cs[-1]

    ivp = (100.0 * sum(1 for v in iv30_hist if v < iv30_now) / len(iv30_hist)
           if iv30_hist else 50.0)
    iv_chg = (iv30_now / iv30_hist[-1] - 1) * 100 if iv30_hist else 0.0
    iv50 = sum(iv30_hist[-50:]) / min(50, len(iv30_hist)) if iv30_hist else iv30_now

    g = 0
    g += 1 if ac20 < -0.05 else -1 if ac20 > 0.05 else 0
    rr = rvcc10 / park10 if park10 else 1.0
    g += 1 if rr < 0.90 else -1 if rr > 1.10 else 0
    g += 1 if spot > e50 else -1
    g += 1 if iv30_now < iv50 else -1

    trend = "RNG" if adx < ADX_THR else "UP" if e8 > e21 else "DN"
    vol = "CMP" if ivp < 25 else "NRM" if ivp < 60 else "ELV" if ivp < 85 else "STR"
    vrp = iv30_now - rv21
    pz = (spot - e21) / atr if atr else 0.0
    bias = max(-2, min(2, (1 if pz > 0.5 else -1 if pz < -0.5 else 0) +
                       (1 if spot > e50 else -1)))
    return {"trend": trend, "vol_state": vol, "iv_pctl": round(ivp, 1),
            "iv30": round(iv30_now, 2), "iv_chg_pct": round(iv_chg, 2),
            "rv7": round(rv7, 2), "rv21": round(rv21, 2),
            "vrp": round(vrp, 2), "rv_falling": rv7 < rv21,
            "gamma_score": g, "gamma": "+g" if g >= 1 else "-g" if g <= -1 else "g?",
            "adx": round(adx, 1), "bias": bias, "ac20": round(ac20, 3),
            "ccrange": round(rr, 3), "spot": spot}


def build_gates(reg: dict, ev: dict, today: date | None = None) -> list[dict]:
    gates = []

    def add(code, msg, hard):
        gates.append({"code": code, "msg": msg, "hard": hard})

    if today is not None and today.weekday() == 4:
        add("W", "Friday session — Monday-entry doctrine: net-debit long-vega "
                 "entries only (calendar/diagonal)", False)

    if abs(reg["iv_chg_pct"]) > IV_SPIKE_PCT:
        add("V", f"IV moved {reg['iv_chg_pct']:+.1f}% today — surface unsettled", True)
    if reg["vol_state"] == "STR":
        add("S", "Vol stressed (IV pctl > 85)", True)
    if reg["gamma"] == "-g" and reg["vol_state"] in ("ELV", "STR"):
        add("G", "Unstable tape + elevated vol", True)
    if ev["fomc_in_front"]:
        add("F", f"FOMC in {ev['fomc_dte']}d — inside front-leg window", False)
    if ev["post_opex"]:
        add("O", "Post-OpEx week — vol expansion window", False)
    if ev["ex_div"]:
        add("X", "ETF ex-div week — no short ITM calls", False)
    if reg["vrp"] <= 0:
        add("P", f"VRP {reg['vrp']:+.1f}v — realized above implied, selling unpaid", False)
    return gates


# -------------------------------------------------------------------- mock --
def mock_bars(symbol: str, spot: float, today: date, n=300, seed=None) -> list:
    # zlib.crc32 is stable across processes; hash() is salted per run and
    # made mock regimes (and the test suite) nondeterministic
    rnd = random.Random(seed if seed is not None else zlib.crc32(symbol.encode()) & 0xffff)
    drift, vol = 0.0003, {"SPX": .010, "SPY": .010, "QQQ": .013,
                          "NDX": .013, "RUT": .014, "IWM": .014}[symbol]
    c = spot / math.exp(n * drift)
    bars = []
    d = today - timedelta(days=int(n * 1.45))
    while len(bars) < n:
        d += timedelta(days=1)
        if d.weekday() > 4:
            continue
        r = rnd.gauss(drift, vol)
        o = c
        c = c * math.exp(r)
        h = max(o, c) * (1 + abs(rnd.gauss(0, vol / 2)))
        l = min(o, c) * (1 - abs(rnd.gauss(0, vol / 2)))
        bars.append((d, o, h, l, c))
    scale = spot / bars[-1][4]
    return [(d, o * scale, h * scale, l * scale, c * scale) for d, o, h, l, c in bars]


def mock_iv_hist(base_iv_pct: float, n=252, seed=7) -> list[float]:
    rnd = random.Random(seed)
    v, out = base_iv_pct, []
    for _ in range(n):
        v = max(8.0, v + rnd.gauss(0, 0.5) + (base_iv_pct - v) * 0.03)
        out.append(round(v, 2))
    return out
