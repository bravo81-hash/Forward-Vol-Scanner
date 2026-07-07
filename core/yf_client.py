"""yfinance fallback — build a Context with no TWS connection.

Purpose-built for the Direction tab: the verdict needs IV level, skew,
term shape, VRP and price bars — not stageable quotes. Data is delayed
~15-20 min and index chains on Yahoo are poor, so:

  * SPX / NDX / RUT are proxied via SPY / QQQ / IWM (surface shape and
    regime inputs transfer; strikes/spot are the proxy's — flagged in
    ctx.data and never staged).
  * IV30 history (for IV rank) comes from the CBOE index-vol series
    ^VIX / ^VXN / ^RVX where one exists. Single names have no free IV
    history: reg["ivp_proxy"]=True and the Direction module falls back
    to the IV30/HAR ratio band, explicitly labelled a proxy.

Anything unusable (missing chain, <2 expiries, dead history) raises
RuntimeError so webapp auto-mode can fall through to mock.
"""
from __future__ import annotations

import math
from datetime import date, datetime

from .chain import SCAN_DTE, k25
from .events import event_flags, trading_today
from .models import Context, Slice
from .pricing import q_for
from .regime import build_gates, compute_regime
from .surface import FRONT_DTE, iv_cm, pair_table, term_stats

PROXY = {"SPX": "SPY", "NDX": "QQQ", "RUT": "IWM"}
IVH_PROXY = {"SPX": "^VIX", "SPY": "^VIX", "NDX": "^VXN", "QQQ": "^VXN",
             "RUT": "^RVX", "IWM": "^RVX"}
MAX_EXPIRIES = 8


def _num(v) -> float | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _row_iv(df, strike: float) -> float | None:
    rows = df[df["strike"] == strike]
    if rows.empty:
        return None
    return _num(rows.iloc[0].get("impliedVolatility"))


def _nearest(strikes: list[float], x: float) -> float:
    return min(strikes, key=lambda s: abs(s - x))


def _slice_from_chain(expiry: date, dte: int, spot: float, calls, puts,
                      qdiv: float) -> Slice | None:
    both = sorted(set(calls["strike"]).intersection(set(puts["strike"])))
    if not both:
        return None
    atm_k = _nearest(both, spot)
    c_iv, p_iv = _row_iv(calls, atm_k), _row_iv(puts, atm_k)
    ivs = [v for v in (c_iv, p_iv) if v]
    if not ivs:
        return None
    atm_iv = sum(ivs) / len(ivs)
    if not 0.02 < atm_iv < 3.0:
        return None

    t = dte / 365.0
    p25k = _nearest(list(puts["strike"]), k25(spot, atm_iv, t, "P", q=qdiv))
    c25k = _nearest(list(calls["strike"]), k25(spot, atm_iv, t, "C", q=qdiv))
    p25_iv = _row_iv(puts, p25k) or 0.0
    c25_iv = _row_iv(calls, c25k) or 0.0

    arow = calls[calls["strike"] == atm_k].iloc[0]
    bid, ask = _num(arow.get("bid")), _num(arow.get("ask"))
    spread = ((ask - bid) / ((ask + bid) / 2)) if bid and ask and ask > bid else 0.10
    oi = int((arow.get("openInterest") or 0)
             + (puts[puts["strike"] == atm_k].iloc[0].get("openInterest") or 0))

    return Slice(expiry=expiry, dte=dte, atm_strike=atm_k, atm_iv=round(atm_iv, 4),
                 put25_iv=round(p25_iv, 4), call25_iv=round(c25_iv, 4),
                 put25_strike=p25k, call25_strike=c25k,
                 atm_spread_pct=round(spread, 3), oi_atm=oi)


def build_context_yf(symbol: str, today: date | None = None) -> Context:
    try:
        import yfinance as yf
    except ImportError as e:
        raise RuntimeError("yfinance not installed — pip install yfinance") from e

    today = today or trading_today()
    symbol = symbol.upper()
    fetch = PROXY.get(symbol, symbol)
    qdiv = q_for(symbol)
    tk = yf.Ticker(fetch)

    hist = tk.history(period="400d", auto_adjust=False)
    if hist is None or len(hist) < 120:
        raise RuntimeError(f"yfinance: insufficient price history for {fetch}")
    bars = [(idx.date() if hasattr(idx, "date") else idx,
             float(r["Open"]), float(r["High"]), float(r["Low"]), float(r["Close"]))
            for idx, r in hist.iterrows()]
    spot = bars[-1][4]

    slices: list[Slice] = []
    for exp in (tk.options or []):
        d = datetime.strptime(exp, "%Y-%m-%d").date()
        dte = (d - today).days
        if not SCAN_DTE[0] <= dte <= SCAN_DTE[1]:
            continue
        try:
            ch = tk.option_chain(exp)
            s = _slice_from_chain(d, dte, spot, ch.calls, ch.puts, qdiv)
        except Exception:
            s = None
        if s:
            slices.append(s)
        if len(slices) >= MAX_EXPIRIES:
            break
    if len(slices) < 2:
        raise RuntimeError(f"yfinance: usable chain too thin for {fetch} "
                           f"({len(slices)} expiries in {SCAN_DTE} DTE)")
    slices.sort(key=lambda s: s.dte)
    iv30 = iv_cm(slices, 30) * 100

    ivh: list[float] = []
    ivp_src = "none — IV30/HAR proxy band"
    vix_sym = IVH_PROXY.get(symbol)
    if vix_sym:
        try:
            vh = yf.Ticker(vix_sym).history(period="1y")
            ivh = [float(c) for c in vh["Close"].tolist() if _num(c)]
            if len(ivh) >= 60:
                ivp_src = f"{vix_sym} history (index IV proxy)"
            else:
                ivh = []
        except Exception:
            ivh = []

    reg = compute_regime(bars, ivh, iv30)
    reg["spot"] = spot
    reg["ivp_src"] = ivp_src
    if not ivh:
        reg["ivp_proxy"] = True

    lo, hi_ = spot * 0.8, spot * 1.2
    strikes = sorted({k for s in slices
                      for k in (s.atm_strike, s.put25_strike, s.call25_strike)
                      if lo <= k <= hi_})

    ev = event_flags(today, symbol, FRONT_DTE[1])
    last = bars[-1][0]
    gap = (today - last).days
    proxied = f" (proxy chain {fetch} for {symbol})" if symbol in PROXY else ""
    data = {"session": str(last), "fresh": gap <= 4, "gap_days": gap,
            "note": f"yfinance — delayed ~15-20 min{proxied}; IVR src: {ivp_src}"}

    ctx = Context(symbol=symbol, spot=spot, today=today, slices=slices,
                  strikes=strikes, regime=reg, events=ev,
                  gates=build_gates(reg, ev, today), mode="yf",
                  q=qdiv, data=data)
    ctx.pairs = pair_table(slices, today)
    ctx.regime["term"] = term_stats(slices)
    return ctx
