#!/usr/bin/env python3
"""webapp.py — TE Playbook trade-selection app (browser UI on :8765).

Per ticker: market context -> regime verdict -> top-2 strategy families ->
4 concrete suggestion cards -> stage to TWS (transmit=False) with whatIf
margin. Management stays in OptionNet Explorer by design.

Modes: mock (no TWS, synthetic surface) / live (TWS via ib_insync).
TWS budget per live refresh per symbol: ~1 underlying + 4 x n_expiry option
lines (batched + cancelled, see core/ib_client.py) + 2 cached hist requests.

Run:  python webapp.py
"""
from __future__ import annotations

from datetime import date

from flask import Flask, jsonify, request, send_from_directory

from core.context import build_context
from core.ib_client import DEFAULT_HOST, DEFAULT_PORT, with_ib
from core.models import Leg
from core.pricing import struct_value
from core.reprice import reprice_cards
from execution.stage import stage_suggestion
from portfolio.accounts import MOCK_ACCOUNTS, list_accounts
from portfolio.book import book_greeks, fetch_positions
from portfolio.risk import book_warnings
from selection.ranker import shortlist
from store.log import log

SYMBOLS = ["SPX", "SPY", "QQQ", "RUT", "IWM"]
app = Flask(__name__, static_folder="static")


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


@app.get("/api/status")
def api_status():
    return jsonify({"symbols": SYMBOLS, "tws": f"{DEFAULT_HOST}:{DEFAULT_PORT}"})


@app.get("/api/accounts")
def api_accounts():
    if request.args.get("mode", "mock") == "mock":
        return jsonify(MOCK_ACCOUNTS)
    try:
        return jsonify(with_ib(list_accounts))
    except Exception as e:                       # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.get("/api/suggest")
def api_suggest():
    symbol = request.args.get("symbol", "SPX").upper()
    mode = request.args.get("mode", "mock")
    account = request.args.get("account") or None
    nlv = request.args.get("nlv", type=float)
    try:
        ctx = build_context(symbol, mode)
        if mode == "live":
            def job(ib):
                return fetch_positions(ib, symbol, account)
            try:
                ctx.book = book_greeks(ctx, with_ib(job))
            except Exception as e:               # book optional, never fatal
                ctx.book = {"error": str(e)}
        if isinstance(ctx.book, dict):
            ctx.book["account"] = account
            ctx.book["nlv"] = nlv
        out = shortlist(ctx)
        if mode == "live" and out["cards"]:
            try:                                 # swap model mids for NBBO
                with_ib(lambda ib: reprice_cards(ib, symbol, ctx.spot,
                                                 ctx.today, out["cards"]))
            except Exception as e:               # keep model prices on failure
                out["reprice_error"] = str(e)
        out["spot"] = ctx.spot
        out["mode"] = mode
        out["book"] = ctx.book
        out["book_warnings"] = book_warnings(ctx.book)
        log("shortlist", symbol, {"verdict": out["verdict"],
                                  "cards": [c["label"] for c in out["cards"]]})
        return jsonify(out)
    except Exception as e:                       # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.post("/api/payoff")
def api_payoff():
    d = request.get_json(force=True)
    spot, today = float(d["spot"]), date.today()
    legs = [Leg(cp=l["cp"], strike=float(l["strike"]),
                expiry=date.fromisoformat(l["expiry"]), qty=int(l["qty"]),
                iv=float(l.get("iv") or 0.18)) for l in d["legs"]]
    entry = (float(d["net_mid"]) if d.get("net_mid") is not None
             else struct_value(spot, legs, today))
    front = min((l.expiry - today).days for l in legs)
    xs, exp, now = [], [], []
    s = spot * 0.90
    while s <= spot * 1.10:
        xs.append(round(s, 1))
        exp.append(round(struct_value(s, legs, today, elapsed=front) - entry, 2))
        now.append(round(struct_value(s, legs, today, elapsed=min(5, front)) - entry, 2))
        s += spot * 0.004
    return jsonify({"x": xs, "expiry": exp, "t5": now, "front_dte": front})


@app.post("/api/stage")
def api_stage():
    d = request.get_json(force=True)
    symbol, legs, net = d["symbol"].upper(), d["legs"], float(d["net_mid"])
    qty = int(d.get("qty", 1))
    account = d.get("account") or None
    if d.get("mode") == "mock":
        log("stage_mock", symbol, d)
        return jsonify({"orderId": -1, "status": "MockStaged", "margin_change": None,
                        "note": "mock mode — nothing sent to TWS"})
    try:
        res = with_ib(lambda ib: stage_suggestion(ib, symbol, legs, net, qty,
                                                  account=account))
        log("stage", symbol, {**d, **res})
        return jsonify(res)
    except Exception as e:                       # noqa: BLE001
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print("TE Playbook app -> http://127.0.0.1:8765   (mock mode needs no TWS)")
    app.run(host="127.0.0.1", port=8765, debug=False)
