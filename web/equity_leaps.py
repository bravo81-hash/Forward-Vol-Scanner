"""Equity LEAPS desk: Radar-B watchlist and Gate E single-name structure selection.

Ported from the pre-blueprint webapp into its own feature blueprint. The heavy
selection/yfinance imports stay lazy inside the handlers so importing the web
package (which the app does eagerly) never pulls them in.
"""
from __future__ import annotations

from flask import Blueprint, jsonify, request, send_from_directory

from web import shared

bp = Blueprint("equity_leaps", __name__)


@bp.get("/equity", strict_slashes=False)
def page_equity():
    return send_from_directory(shared.STATIC_DIR, "equity_leaps.html")


@bp.get("/api/equity/gate-e")
def api_equity_gate_e():
    """Gate E: single-name equity structure selection."""
    from core.stock_data import earnings_date_yf
    from selection import cards_e, gate_e, radar_b
    from selection.equity_context import EquityThrottleError
    from selection.equity_context import build as build_equity

    symbol = request.args.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    hold = request.args.get("hold", "medium").lower()
    if hold not in gate_e.HOLDS:
        return jsonify({"error": f"bad hold '{hold}'"}), 400
    verify_trigger = request.args.get("trigger", "0") in ("1", "true", "yes")
    source = request.args.get("source", "auto").lower()
    if source not in {"auto", "live", "yf"}:
        return jsonify({"error": f"bad source '{source}'"}), 400

    errors, ctx, bars, throttled = [], None, [], False
    try:
        ctx, bars = build_equity(symbol, hold, source=source)
    except EquityThrottleError as e:
        throttled = True
        errors.append(f"{type(e).__name__}: {e}")
    except Exception as e:                           # noqa: BLE001
        errors.append(f"{type(e).__name__}: {e}")
    if ctx is None:
        return jsonify({
            "error": ("yfinance is rate-limiting - wait about a minute and "
                      "retry; the result is cached once it succeeds"
                      if throttled else "no usable chain for this symbol"),
            "throttled": throttled, "errors": errors}), 502

    trigger = None
    if verify_trigger:
        try:
            earnings = earnings_date_yf(symbol)
            if earnings is None:
                errors.append("Trigger verification blocked: next earnings date is unavailable")
                trigger = {"fired": False, "checks": [
                    "The reclaim trigger was not verified because the next earnings "
                    "date is unavailable, so the earnings-blackout condition cannot pass."],
                    "level": None}
            else:
                trigger = radar_b.trigger(radar_b.base_metrics(symbol, bars), bars,
                                          earnings=earnings)
        except Exception as exc:                    # noqa: BLE001
            errors.append(f"Trigger verification failed: {type(exc).__name__}: {exc}")
            trigger = {"fired": False, "checks": [
                "The reclaim trigger could not be verified from complete price, volume "
                "and earnings data, so it remains blocked."], "level": None}
    payload = gate_e.build(ctx, hold,
                           trigger_fired=bool(trigger and trigger.get("fired")), bars=bars)
    payload["trigger"] = trigger
    payload["card"] = cards_e.render(payload, trigger=trigger)
    payload["source"] = (ctx.data or {}).get("chain_source", "unknown")
    payload["errors"] = errors
    return jsonify(payload)


@bp.get("/api/equity/radar")
def api_equity_radar():
    """Radar-B watchlist. NOT signals - entries fire on the separate trigger."""
    from core.fundamentals import fetch_many
    from core.stock_data import histories_yf
    from selection import radar_b
    from selection.stock_radar import load_universe

    limit = int(request.args.get("limit", radar_b.OUTPUT_LIMIT))
    syms = [u["symbol"] for u in load_universe()]
    only = request.args.get("symbols")
    if only:
        want = {s.strip().upper() for s in only.split(",") if s.strip()}
        syms = [s for s in syms if s in want] or sorted(want)

    bars = histories_yf(syms, period="2y")
    bench = histories_yf(["SPY"], period="2y").get("SPY")
    funds = fetch_many([s for s in syms if s in bars])
    out = radar_b.scan(bars, funds, bench_bars=bench, limit=limit)
    out["watchlist"] = [{
        "symbol": r["symbol"], "score": r["score"],
        "score_parts": r.get("score_parts", {}), "reasons": r["reasons"],
        "metrics": {k: v for k, v in r["metrics"].__dict__.items()
                    if not k.startswith("_") and k != "blocks"},
    } for r in out["watchlist"]]
    return jsonify(out)
