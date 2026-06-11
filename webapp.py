#!/usr/bin/env python3
"""
webapp.py — Browser dashboard for the forward-vol scanner.

Serves a visual UI on http://127.0.0.1:8765 with:
  * mock / live scans per surface (SPX / NDX / RUT)
  * term-structure chart + ranked calendar pairs with event context
  * on-demand risk profile (Black-Scholes model P&L at front expiry)
  * "send to TWS as held" — places the calendar combo with transmit=False,
    so it arrives in TWS untransmitted for manual review.

Run:  python webapp.py            (TWS only needed for live scans / orders)
"""

from __future__ import annotations

import asyncio
import itertools
import math
import threading
from datetime import date, datetime

from flask import Flask, jsonify, request, send_from_directory

from fwdvol_scanner import (
    DEFAULT_HOST, DEFAULT_PORT, SURFACES,
    build_pairs, fetch_ivs_ib, forward_vol, synthetic_surface,
)

TWS_HOST = DEFAULT_HOST
TWS_PORT = DEFAULT_PORT
_client_ids = itertools.cycle(range(32, 64))   # CLI scanner uses 31; rotate to
                                               # avoid TWS clientId-release lag
WEB_PORT = 8765
MULTIPLIER = 100                 # SPX/NDX/RUT index options
RISK_FREE = 0.04

SYNTH = {  # mirror of fwdvol_scanner.main(): base 30d IV, slope, event kink
    "SPX": (0.14, 0.020, True),
    "RUT": (0.20, 0.015, False),
    "SPY": (0.14, 0.020, True),
    "QQQ": (0.18, 0.022, True),
    "IWM": (0.20, 0.015, False),
}
MOCK_SPOT = {"SPX": 6000.0, "RUT": 2300.0, "SPY": 600.0, "QQQ": 525.0, "IWM": 230.0}

app = Flask(__name__, static_folder="static")

_ib_lock = threading.Lock()                # one TWS session at a time
_cache: dict[tuple[str, str], dict] = {}   # (symbol, mode) -> scan snapshot


# ---------------------------------------------------------------- IB helper --

def with_ib(fn):
    """Run fn(ib) on a fresh connection in its own thread + event loop."""
    out: dict = {}

    def target():
        asyncio.set_event_loop(asyncio.new_event_loop())
        from ib_insync import IB
        ib = IB()
        try:
            ib.connect(TWS_HOST, TWS_PORT, clientId=next(_client_ids), timeout=10)
            out["value"] = fn(ib)
        except Exception as e:  # noqa: BLE001 - surfaced to the API caller
            out["error"] = e if str(e) else RuntimeError(type(e).__name__)
        finally:
            if ib.isConnected():
                ib.disconnect()

    with _ib_lock:
        t = threading.Thread(target=target, daemon=True)
        t.start()
        t.join(timeout=180)
    if "error" in out:
        raise out["error"]
    if "value" not in out:
        raise RuntimeError("TWS request timed out")
    return out["value"]


# ------------------------------------------------------------------- math ---

def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_call(s: float, k: float, t: float, sigma: float, r: float = RISK_FREE) -> float:
    if t <= 0:
        return max(s - k, 0.0)
    if sigma <= 0:
        return max(s - k * math.exp(-r * t), 0.0)
    sq = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / sq
    d2 = d1 - sq
    return s * _norm_cdf(d1) - k * math.exp(-r * t) * _norm_cdf(d2)


def bs_greeks(s: float, k: float, t: float, sigma: float,
              r: float = RISK_FREE) -> dict[str, float]:
    """Call greeks: delta, gamma, theta (per day), vega (per 1 vol pt)."""
    if t <= 0 or sigma <= 0:
        return {"delta": 1.0 if s > k else 0.0, "gamma": 0.0,
                "theta": 0.0, "vega": 0.0}
    sq = sigma * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * sigma * sigma) * t) / sq
    d2 = d1 - sq
    pdf = math.exp(-0.5 * d1 * d1) / math.sqrt(2.0 * math.pi)
    theta_yr = (-s * pdf * sigma / (2.0 * math.sqrt(t))
                - r * k * math.exp(-r * t) * _norm_cdf(d2))
    return {"delta": _norm_cdf(d1), "gamma": pdf / (s * sq),
            "theta": theta_yr / 365.0, "vega": s * pdf * math.sqrt(t) / 100.0}


def _round_tick(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, 2)


def _leg_quotes(ib, symbol: str, tc: str, fe: date, be: date, strike: float) -> dict:
    """Live bid/ask/mid for both legs + combo mid debit."""
    from ib_insync import Option
    surf = SURFACES[symbol]
    legs = [Option(symbol, d.strftime("%Y%m%d"), strike, "C", surf.opt_exchange,
                   tradingClass=tc, currency=surf.currency) for d in (fe, be)]
    ib.qualifyContracts(*legs)
    qf, qb = legs
    if not qf.conId or not qb.conId:
        raise RuntimeError("could not qualify legs for quotes")
    tf = ib.reqMktData(qf, "", snapshot=False)
    tb = ib.reqMktData(qb, "", snapshot=False)

    def val(v):
        return v if v is not None and not math.isnan(v) and v > 0 else None

    waited = 0.0
    while waited < 8.0:
        ib.sleep(0.5)
        waited += 0.5
        if all(val(t.bid) and val(t.ask) for t in (tf, tb)):
            break

    def ba(tk):
        bid, ask = val(tk.bid), val(tk.ask)
        mid = round((bid + ask) / 2.0, 2) if bid and ask else None
        return {"bid": bid, "ask": ask, "mid": mid}

    out = {"front": ba(tf), "back": ba(tb)}
    ib.cancelMktData(qf)
    ib.cancelMktData(qb)
    fm, bm = out["front"]["mid"], out["back"]["mid"]
    out["midDebit"] = round(bm - fm, 2) if fm and bm else None
    return out


def edge_tag(edge: float) -> str:
    return "CHEAP FWD" if edge > 1.0 else "MARGINAL" if edge > 0 else "NO CAL EDGE"


# ------------------------------------------------------------------ routes ---

@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/status")
def api_status():
    import socket
    s = socket.socket()
    s.settimeout(1.0)
    ok = s.connect_ex((TWS_HOST, TWS_PORT)) == 0
    s.close()
    return jsonify({"tws": ok, "host": TWS_HOST, "port": TWS_PORT})


@app.get("/api/scan")
def api_scan():
    symbol = request.args.get("symbol", "SPX").upper()
    mode = request.args.get("mode", "mock")
    if symbol not in SURFACES:
        return jsonify({"error": f"unknown symbol {symbol}"}), 400

    today = date.today()
    try:
        if mode == "mock":
            spot = MOCK_SPOT[symbol]
            atm = spot
            tc = SURFACES[symbol].trading_class or symbol
            ivs = synthetic_surface(today, *SYNTH[symbol])
        else:
            spot, atm, tc, ivs = with_ib(
                lambda ib: fetch_ivs_ib(ib, SURFACES[symbol], today))
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    _cache[(symbol, mode)] = {
        "spot": spot, "atm": atm, "tc": tc, "ivs": ivs,
        "ts": datetime.now().isoformat(timespec="seconds"),
    }

    df = build_pairs(ivs, today)
    pairs = []
    if not df.empty:
        for _, r in df.iterrows():
            pairs.append({
                "front": r["front"], "fdte": int(r["fDTE"]), "fiv": float(r["fIV%"]),
                "back": r["back"], "bdte": int(r["bDTE"]), "biv": float(r["bIV%"]),
                "fwd": None if math.isnan(r["fwdVol%"]) else float(r["fwdVol%"]),
                "edge": None if math.isnan(r["edge(f-fwd)"]) else float(r["edge(f-fwd)"]),
                "events": r["events"],
                "tag": edge_tag(r["edge(f-fwd)"]) if not math.isnan(r["edge(f-fwd)"]) else "N/A",
            })

    term = [{"date": d.isoformat(), "dte": (d - today).days, "iv": round(v * 100, 2)}
            for d, v in sorted(ivs.items())]
    return jsonify({
        "symbol": symbol, "mode": mode, "spot": spot, "atm": atm,
        "tradingClass": tc, "term": term, "pairs": pairs,
        "ts": _cache[(symbol, mode)]["ts"],
    })


@app.get("/api/risk")
def api_risk():
    symbol = request.args.get("symbol", "").upper()
    mode = request.args.get("mode", "mock")
    snap = _cache.get((symbol, mode))
    if snap is None:
        return jsonify({"error": "run a scan first"}), 400
    try:
        fe = datetime.strptime(request.args["front"], "%Y-%m-%d").date()
        be = datetime.strptime(request.args["back"], "%Y-%m-%d").date()
    except (KeyError, ValueError):
        return jsonify({"error": "front/back must be YYYY-MM-DD"}), 400
    if fe not in snap["ivs"] or be not in snap["ivs"]:
        return jsonify({"error": "expiry not in last scan"}), 400

    today = date.today()
    spot, strike = snap["spot"], snap["atm"]
    t1, t2 = (fe - today).days / 365.0, (be - today).days / 365.0
    iv1, iv2 = snap["ivs"][fe], snap["ivs"][be]
    fwd = forward_vol(iv1, t1, iv2, t2)
    if not math.isfinite(fwd):
        return jsonify({"error": "inverted/invalid forward vol for this pair"}), 400

    # entry debit: long back call, short front call, both at ATM strike
    debit = bs_call(spot, strike, t2, iv2) - bs_call(spot, strike, t1, iv1)
    rem = t2 - t1

    width = max(4.0 * iv1 * math.sqrt(t1), 0.02)
    lo, hi = spot * (1 - width), spot * (1 + width)
    n = 121
    points, pnls = [], []
    for i in range(n):
        s = lo + (hi - lo) * i / (n - 1)
        val = bs_call(s, strike, rem, fwd) - max(s - strike, 0.0)
        pnl = (val - debit) * MULTIPLIER
        points.append({"s": round(s, 1), "pnl": round(pnl, 0)})
        pnls.append(pnl)

    breakevens = []
    for i in range(1, n):
        a, b = pnls[i - 1], pnls[i]
        if (a < 0 <= b) or (a >= 0 > b):
            sa = points[i - 1]["s"]
            sb = points[i]["s"]
            breakevens.append(round(sa + (sb - sa) * (-a) / (b - a), 1))

    max_profit = max(pnls)
    max_loss = min(pnls)

    # net position greeks per spread: long back call, short front call
    gf = bs_greeks(spot, strike, t1, iv1)
    gb = bs_greeks(spot, strike, t2, iv2)
    greeks = {
        "delta": round((gb["delta"] - gf["delta"]) * MULTIPLIER, 1),
        "gamma": round((gb["gamma"] - gf["gamma"]) * MULTIPLIER, 4),
        "theta": round((gb["theta"] - gf["theta"]) * MULTIPLIER, 0),
        "vega": round((gb["vega"] - gf["vega"]) * MULTIPLIER, 0),
    }

    # live combo mid for the limit price; fall back to model debit
    legs, legs_error = None, None
    if mode == "live":
        try:
            legs = with_ib(lambda ib: _leg_quotes(ib, symbol, snap["tc"], fe, be, strike))
        except Exception as e:  # noqa: BLE001 - report, don't fail the profile
            legs_error = str(e)
    mid_debit = legs.get("midDebit") if legs else None
    limit_suggest = _round_tick(mid_debit if mid_debit else debit)
    limit_src = "mid" if mid_debit else "model"

    return jsonify({
        "symbol": symbol, "front": fe.isoformat(), "back": be.isoformat(),
        "strike": strike, "spot": spot,
        "fiv": round(iv1 * 100, 2), "biv": round(iv2 * 100, 2),
        "fwd": round(fwd * 100, 2),
        "debit": round(debit, 2), "debitDollars": round(debit * MULTIPLIER, 0),
        "maxProfit": round(max_profit, 0), "maxLoss": round(max_loss, 0),
        "rr": round(max_profit / abs(max_loss), 2) if max_loss < 0 else None,
        "breakevens": breakevens, "points": points,
        "greeks": greeks, "legs": legs, "legsError": legs_error,
        "midDebit": mid_debit, "limitSuggest": limit_suggest, "limitSrc": limit_src,
        "assumptions": (f"Model estimate at front expiry ({fe.isoformat()}): "
                        f"Black-Scholes, r={RISK_FREE:.0%}, back leg repriced at "
                        f"forward vol {fwd*100:.1f}%. ATM {strike:g} call calendar, "
                        f"per 1 spread (x{MULTIPLIER}). Excludes fees/slippage; "
                        "IV shifts will move the curve."),
    })


@app.post("/api/order")
def api_order():
    body = request.get_json(force=True)
    symbol = str(body.get("symbol", "")).upper()
    mode = str(body.get("mode", "mock"))
    if mode != "live":
        return jsonify({"error": "orders only available in LIVE mode"}), 400
    snap = _cache.get((symbol, "live"))
    if snap is None:
        return jsonify({"error": "run a live scan first"}), 400
    try:
        front = str(body["front"]).replace("-", "")
        back = str(body["back"]).replace("-", "")
        strike = float(body["strike"])
        qty = int(body["qty"])
        limit = round(float(body["limit"]), 2)
    except (KeyError, ValueError) as e:
        return jsonify({"error": f"bad order fields: {e}"}), 400
    if qty < 1 or qty > 10:
        return jsonify({"error": "qty must be 1-10"}), 400
    if limit <= 0:
        return jsonify({"error": "limit debit must be positive"}), 400

    surf = SURFACES[symbol]
    tc = snap["tc"]

    def place(ib):
        from ib_insync import ComboLeg, Contract, LimitOrder, Option
        leg_f = Option(symbol, front, strike, "C", surf.opt_exchange,
                       tradingClass=tc, currency=surf.currency)
        leg_b = Option(symbol, back, strike, "C", surf.opt_exchange,
                       tradingClass=tc, currency=surf.currency)
        ib.qualifyContracts(leg_f, leg_b)
        qf, qb = leg_f, leg_b
        if not qf.conId or not qb.conId:
            raise RuntimeError("could not qualify option legs")
        combo = Contract(secType="BAG", symbol=symbol,
                         currency=surf.currency, exchange=surf.opt_exchange)
        combo.comboLegs = [
            ComboLeg(conId=qf.conId, ratio=1, action="SELL", exchange=surf.opt_exchange),
            ComboLeg(conId=qb.conId, ratio=1, action="BUY", exchange=surf.opt_exchange),
        ]
        order = LimitOrder("BUY", qty, limit, transmit=False,
                           orderRef="fwdvol-dashboard")
        trade = ib.placeOrder(combo, order)
        ib.sleep(1.5)
        return {"orderId": trade.order.orderId,
                "status": trade.orderStatus.status or "Sent (held)"}

    try:
        result = with_ib(place)
    except Exception as e:
        return jsonify({"error": str(e)}), 502
    result["note"] = ("Order delivered to TWS UNTRANSMITTED (held). "
                      "Open TWS, review it, and press Transmit to send it live.")
    return jsonify(result)


if __name__ == "__main__":
    print(f"Forward-vol dashboard ->  http://127.0.0.1:{WEB_PORT}")
    app.run(host="127.0.0.1", port=WEB_PORT, threaded=True)
