"""Assemble a Context per ticker — ONE place that touches data sources.

TWS budget per live refresh, per symbol:
  1 underlying snapshot + ~4 x n_expiries option lines (batched, cancelled)
  + 2 cached historical requests (daily bars, IV30 history; 1h TTL)
"""
from __future__ import annotations
from datetime import date

from .chain import SURFACE_CFG, build_chain_live, build_chain_mock
from .events import event_flags, trading_today
from .ib_client import BARS_CACHE, daily_bars, with_ib
from .models import Context
from .regime import build_gates, compute_regime, mock_bars, mock_iv_hist
from .pricing import q_for
from .surface import FRONT_DTE, iv_cm, pair_table, term_stats


def build_context(symbol: str, mode: str = "mock", today: date | None = None,
                  host=None, port=None) -> Context:
    today = today or trading_today()
    if mode == "mock":
        spot, slices, strikes = build_chain_mock(symbol, today)
        bars = mock_bars(symbol, spot, today)
        iv30 = iv_cm(slices, 30) * 100
        ivh = mock_iv_hist(iv30 * 0.96)
    else:
        def job(ib):
            sp, sl, ks = build_chain_live(ib, symbol, today)
            from ib_insync import Index, Stock
            st, exch, tc, is_idx = SURFACE_CFG[symbol]
            und = Index(symbol, exch, "USD") if is_idx else Stock(symbol, "SMART", "USD")
            ib.qualifyContracts(und)
            brs = daily_bars(ib, und)
            key = ("ivh", symbol)
            ivh_ = BARS_CACHE.get(key)
            if ivh_ is None:
                hb = ib.reqHistoricalData(und, "", "252 D", "1 day",
                                          "OPTION_IMPLIED_VOLATILITY",
                                          useRTH=True, formatDate=1)
                ivh_ = [b.close * 100 for b in hb]
                BARS_CACHE.put(key, ivh_)
            return sp, sl, ks, brs, ivh_
        kw = {}
        if host:
            kw["host"] = host
        if port:
            kw["port"] = port
        spot, slices, strikes, bars, ivh = with_ib(job, **kw)
        iv30 = (iv_cm(slices, 30) * 100
                if slices else (ivh[-1] if ivh else 15.0))

    reg = compute_regime(bars, ivh, iv30)
    reg["spot"] = spot
    ev = event_flags(today, symbol, FRONT_DTE[1])
    # F3: session-freshness guard. `bars` are useRTH daily; the newest bar is
    # the last completed session. Flag when it lags the expected session so a
    # holiday/half-day/weekend run can't serve stale numbers as "today".
    data = _freshness(bars, today, mode)
    ctx = Context(symbol=symbol, spot=spot, today=today, slices=slices,
                  strikes=strikes, regime=reg, events=ev,
                  gates=build_gates(reg, ev, today), mode=mode,
                  q=q_for(symbol), data=data)
    ctx.pairs = pair_table(slices, today)
    ctx.regime["term"] = term_stats(slices)
    return ctx


def _freshness(bars, today: date, mode: str) -> dict:
    if mode == "mock":
        return {"session": today.isoformat(), "fresh": True, "note": "mock data"}
    if not bars:
        return {"session": None, "fresh": False, "note": "no bars returned — TWS data feed?"}
    last = bars[-1][0]
    last = last.date() if hasattr(last, "date") else last
    gap = (today - last).days if hasattr(last, "__sub__") else None
    # >4 calendar days behind = a clear multi-session stale gap (weekend +
    # holiday tolerated); intraday-before-close (last bar = yesterday) is fine.
    stale = gap is not None and gap > 4
    return {"session": str(last), "fresh": not stale, "gap_days": gap,
            "note": (f"STALE — newest bar is {gap}d old; verify the session/feed"
                     if stale else f"latest session {last}")}
