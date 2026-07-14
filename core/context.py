"""Assemble a Context per ticker — ONE place that touches data sources.

TWS budget per live refresh, per symbol:
  1 underlying snapshot + ~4 x n_expiries option lines (batched, cancelled)
  + 2 cached historical requests (daily bars, IV30 history; 1h TTL)
"""
from __future__ import annotations
from datetime import date

from .chain import SURFACE_CFG, build_chain_live, build_chain_mock
from .events import event_flags, trading_clock, trading_today
from .ib_client import BARS_CACHE, daily_bars, with_ib
from .models import Context
from .regime import build_gates, compute_regime, mock_bars, mock_iv_hist
from .pricing import q_for
from .surface import FRONT_DTE, iv_cm, pair_table, term_stats


def build_context(symbol: str, mode: str = "mock", today: date | None = None,
                  host=None, port=None, manual: dict | None = None) -> Context:
    today = today or trading_today()
    manual = manual or {}
    if mode == "mock":
        spot, slices, strikes = build_chain_mock(
            symbol, today,
            spot_override=manual.get("spot"),
            iv30_override=(float(manual["iv30"]) / 100 if manual.get("iv30") else None),
            rr25_override=manual.get("rr25_30d"),
            term_override=manual.get("term"),
        )
        bars = mock_bars(symbol, spot, today)
        iv30 = iv_cm(slices, 30) * 100
        ivh = mock_iv_hist(iv30 * 0.96)
    else:
        def job(ib):
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
            if len(brs) < 150:
                raise RuntimeError(f"{symbol}: TWS returned only {len(brs)} daily bars; "
                                   "check historical-data permissions")
            clock = trading_clock()
            ib.reqMarketDataType(1 if clock["regular_session"] else 2)
            diag = {"market_data_type": "live" if clock["regular_session"] else "frozen"}
            fallback_iv = (ivh_[-1] / 100 if ivh_ else None)
            sp, sl, ks = build_chain_live(
                ib, symbol, today, fallback_spot=brs[-1][4],
                fallback_iv=fallback_iv, diagnostics=diag)
            return sp, sl, ks, brs, ivh_, diag
        kw = {}
        if host:
            kw["host"] = host
        if port:
            kw["port"] = port
        result = with_ib(job, **kw)
        if not isinstance(result, tuple) or len(result) != 6:
            raise RuntimeError("TWS context returned an incomplete result; reconnect and retry")
        spot, slices, strikes, bars, ivh, live_diag = result
        iv30 = (iv_cm(slices, 30) * 100
                if slices else (ivh[-1] if ivh else 15.0))

    reg = compute_regime(bars, ivh, iv30)
    reg["spot"] = spot
    ev = event_flags(today, symbol, FRONT_DTE[1])
    # F3: session-freshness guard. `bars` are useRTH daily; the newest bar is
    # the last completed session. Flag when it lags the expected session so a
    # holiday/half-day/weekend run can't serve stale numbers as "today".
    data = _freshness(bars, today, mode)
    if mode == "live":
        data.update(live_diag)
    ctx = Context(symbol=symbol, spot=spot, today=today, slices=slices,
                  strikes=strikes, regime=reg, events=ev,
                  gates=build_gates(reg, ev, today), mode=mode,
                  q=q_for(symbol), data=data)
    ctx.pairs = pair_table(slices, today)
    ctx.regime["term"] = term_stats(slices)
    if manual:
        _apply_manual_context(ctx, manual)
    return ctx


def _apply_manual_context(ctx: Context, manual: dict) -> None:
    """Apply values read by the user from ONE for one historical test session."""
    reg = ctx.regime
    band = manual.get("iv_band")
    if band:
        reg["vol_state"] = band
        reg["iv_pctl"] = {"CMP": 15.0, "NRM": 45.0,
                          "ELV": 72.0, "STR": 90.0}[band]
    for key in ("iv30", "rv21", "vrp_fwd"):
        if manual.get(key) is not None:
            reg[key] = round(float(manual[key]), 2)
    if manual.get("rv21") is not None:
        reg["har_rv"] = round(float(manual["rv21"]), 2)
    if manual.get("vrp_fwd") is not None:
        reg["vrp"] = round(float(manual["vrp_fwd"]), 2)
        reg["vrp_flip"] = False
    if manual.get("bias") is not None:
        reg["bias"] = int(manual["bias"])
    if manual.get("trend"):
        reg["trend"] = manual["trend"]
    reg["gamma"] = ("-g" if reg.get("vol_state") == "STR" or
                    manual.get("term") == "INVERTED FRONT" else
                    "+g" if reg.get("trend") == "RNG" else "g?")
    term = reg.setdefault("term", {})
    if manual.get("term"):
        term["verdict"] = manual["term"]
    if manual.get("rr25_30d") is not None:
        rr = round(float(manual["rr25_30d"]), 2)
        term["rr25_30d"] = rr
        term["skew_rich"] = rr / max(float(reg.get("iv30") or 1), 1) > 0.30

    event = manual.get("event", "NONE")
    if event == "NONE":
        for key in ("opex_week", "fomc_in_front", "macro_in_front", "post_opex"):
            ctx.events[key] = False
    elif event == "OPEX":
        ctx.events["opex_week"] = True
        ctx.events["opex_date"] = min(ctx.slices, key=lambda s: s.dte).expiry.isoformat()
    elif event == "FOMC":
        ctx.events["fomc_in_front"] = True
        ctx.events["fomc_dte"] = min(14, min(s.dte for s in ctx.slices))
    elif event == "MACRO":
        ctx.events["macro_in_front"] = True
        ctx.events["macro_type"] = "Manual Tier-1 event"
        ctx.events["macro_dte"] = min(7, min(s.dte for s in ctx.slices))

    ctx.gates = build_gates(reg, ctx.events, ctx.today)
    ctx.data = {"session": ctx.today.isoformat(), "fresh": True,
                "historical": bool(manual.get("historical", True)),
                "source": "manual_optionnet_context",
                "as_of_time": manual.get("as_of_time", "15:30"),
                "note": "Market state supplied from OptionNet Explorer"}


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
