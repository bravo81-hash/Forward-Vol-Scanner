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

from datetime import date, timedelta

from flask import Flask, jsonify, request, send_from_directory

from core.context import build_context
from core.events import trading_today
from core.ib_client import DEFAULT_HOST, DEFAULT_PORT, with_ib
from core.models import Leg
from core.pricing import struct_value, q_for
from core.reprice import reprice_cards
from core.surface import term_stats
from core.walls import scan_walls
from execution.stage import stage_suggestion
from portfolio.accounts import MOCK_ACCOUNTS, list_accounts
from portfolio.book import book_greeks, fetch_positions, stress_book
from portfolio.risk import book_warnings
from selection.ranker import shortlist
from store.log import log, log_scan

import sentinel as S

SYMBOLS = ["SPX", "SPY", "QQQ", "RUT", "IWM"]

# Account id(s) that are SMSF / cash-settled — Sentinel applies the EU cash-index
# multi-expiry block to these. Add your real SMSF id here once; leave empty and
# every account is treated as a margin/trading book.
SENTINEL_INVESTING_ACCOUNTS: set[str] = set()

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


@app.get("/api/direction")
def api_direction():
    """Direction tab: objective structure selection for a stated intent.

    symbol: any ticker (SURFACE_CFG symbols usable live; anything else
            resolves via yfinance).
    intent: long | short | vol | auto   (auto = regime bias decides side)
    mode:   auto | live | yf | mock     (auto = TWS -> yfinance -> mock)
    """
    from core.chain import MOCK, SURFACE_CFG
    from core.yf_client import build_context_yf
    from selection.direction import direction_verdict

    symbol = request.args.get("symbol", "SPX").upper().strip()
    intent = request.args.get("intent", "auto").lower()
    mode = request.args.get("mode", "auto").lower()
    if intent not in ("long", "short", "vol", "auto"):
        return jsonify({"error": f"bad intent '{intent}'"}), 400

    errors, ctx = [], None
    order = {"live": ["live"], "yf": ["yf"], "mock": ["mock"],
             "auto": (["live", "yf", "mock"] if symbol in SURFACE_CFG
                      else ["yf"])}.get(mode)
    if order is None:
        return jsonify({"error": f"bad mode '{mode}'"}), 400
    for m in order:
        if m == "live" and symbol not in SURFACE_CFG:
            errors.append("live: symbol not in SURFACE_CFG")
            continue
        if m == "mock" and symbol not in MOCK:
            errors.append("mock: no synthetic surface for symbol")
            continue
        try:
            ctx = (build_context_yf(symbol) if m == "yf"
                   else build_context(symbol, m))
            break
        except Exception as e:                   # noqa: BLE001
            errors.append(f"{m}: {e}")
    if ctx is None:
        return jsonify({"error": "; ".join(errors) or "no data source"}), 502

    out = direction_verdict(ctx, intent)
    if errors:
        out["fallback_chain"] = errors
    log("direction", symbol, {"intent": intent, "mode": ctx.mode,
                              "play": out["play"], "side": out["side"],
                              "top": (out["structures"][0]["key"]
                                      if out["structures"] else None)})
    return jsonify(out)


@app.get("/api/smsf")
def api_smsf():
    """SMSF tab: single-expiry structure selection for the cash account.

    symbol: SPX / RUT (any SURFACE_CFG symbol accepted; index advisory)
    intent: auto | bull | neutral | bear   (auto = regime bias decides)
    mode:   auto | live | yf | mock
    """
    from core.chain import MOCK, SURFACE_CFG
    from core.yf_client import build_context_yf
    from selection.smsf import smsf_verdict

    symbol = request.args.get("symbol", "SPX").upper().strip()
    intent = request.args.get("intent", "auto").lower()
    mode = request.args.get("mode", "auto").lower()
    if intent not in ("auto", "bull", "neutral", "bear"):
        return jsonify({"error": f"bad intent '{intent}'"}), 400

    errors, ctx = [], None
    order = {"live": ["live"], "yf": ["yf"], "mock": ["mock"],
             "auto": (["live", "yf", "mock"] if symbol in SURFACE_CFG
                      else ["yf"])}.get(mode)
    if order is None:
        return jsonify({"error": f"bad mode '{mode}'"}), 400
    for m in order:
        if m == "live" and symbol not in SURFACE_CFG:
            errors.append("live: symbol not in SURFACE_CFG")
            continue
        if m == "mock" and symbol not in MOCK:
            errors.append("mock: no synthetic surface for symbol")
            continue
        try:
            ctx = (build_context_yf(symbol) if m == "yf"
                   else build_context(symbol, m))
            break
        except Exception as e:                   # noqa: BLE001
            errors.append(f"{m}: {e}")
    if ctx is None:
        return jsonify({"error": "; ".join(errors) or "no data source"}), 502

    out = smsf_verdict(ctx, intent)
    if errors:
        out["fallback_chain"] = errors
    log("smsf", symbol, {"intent": intent, "mode": ctx.mode,
                         "bias": out["bias"],
                         "top": (out["structures"][0]["key"]
                                 if out["structures"] else None)})
    return jsonify(out)


@app.get("/api/suggest")
def api_suggest():
    symbol = request.args.get("symbol", "SPX").upper()
    mode = request.args.get("mode", "mock")
    account = request.args.get("account") or None
    nlv = request.args.get("nlv", type=float)
    try:
        ctx = build_context(symbol, mode)
        # F1: per-account mandate — SMSF/investing books cannot hold multi-expiry
        # combos on EU cash-settled indices; the ranker drops+flags those.
        investing = account in SENTINEL_INVESTING_ACCOUNTS
        ctx.mandate = {"account": account, "investing": investing,
                       "block_multi_expiry": investing and symbol in S.EU_CASH_INDEX}
        if mode == "live":
            def job(ib):
                return fetch_positions(ib, symbol, account, with_greeks=True)  # F2
            try:
                pos = with_ib(job)
                ctx.book = book_greeks(ctx, pos)
                ctx.book["stress"] = stress_book(ctx, pos)
            except Exception as e:               # book optional, never fatal
                ctx.book = {"error": str(e)}
        if isinstance(ctx.book, dict):
            ctx.book["account"] = account
            ctx.book["nlv"] = nlv
        out = shortlist(ctx)
        if mode == "live" and out["cards"]:
            def enrich(ib):                      # one connection for both
                reprice_cards(ib, symbol, ctx.spot, ctx.today, out["cards"])
                return scan_walls(ib, symbol, ctx, out["cards"])
            try:                                 # NBBO mids + OI walls
                out["walls"] = with_ib(enrich)
            except Exception as e:               # keep model values on failure
                out["enrich_error"] = str(e)
        out["spot"] = ctx.spot
        out["mode"] = mode
        out["book"] = ctx.book
        out["book_warnings"] = book_warnings(ctx.book)
        log("shortlist", symbol, {"verdict": out["verdict"],
                                  "cards": [c["label"] for c in out["cards"]]})
        log_scan(out, account, mode)             # P3: structured, queryable row
        return jsonify(out)
    except Exception as e:                       # noqa: BLE001
        return jsonify({"error": str(e)}), 500


def _sentinel_mock_positions(spot: float) -> list[dict]:
    """Synthetic DEMO book: a put-heavy, near-dated short strangle (directional
    +delta, short vega, <=7 DTE -> gamma flag) so mock mode actually exercises
    Sentinel's conflict + suggestion UI. Only the positions are fabricated —
    Greeks still come from the real book_greeks pipeline."""
    f = (trading_today() + timedelta(days=6)).strftime("%Y%m%d")
    return [{"cp": "P", "strike": round(spot * 0.97), "expiry": f, "qty": -3, "conId": 0},
            {"cp": "C", "strike": round(spot * 1.05), "expiry": f, "qty": -1, "conId": 0}]


def _sentinel_payload(cards) -> list[dict]:
    """Serialize Sentinel guidance cards to JSON-safe dicts (enums -> values)."""
    def play(p):
        return {"family": p.family, "side": p.side.value,
                "intent": p.intent, "note": p.note}

    def conf(c):
        return {"name": c.name, "message": c.message,
                "severity": c.severity, "need": c.need}

    def sug(s):
        return {"family": s.family, "side": s.side.value, "intent": s.intent,
                "note": s.note, "fix_score": s.fix_score,
                "blocked": s.blocked, "block_reason": s.block_reason}

    return [{"account": c.account, "label": c.label, "pool": c.pool,
             "greeks": c.greeks, "budget": c.budget, "aligned": c.aligned,
             "conflicts": [conf(x) for x in c.conflicts],
             "suggestions": [sug(x) for x in c.suggestions],
             "standing_plays": [play(x) for x in c.standing_plays]}
            for c in cards]


@app.get("/api/sentinel")
def api_sentinel():
    """Portfolio-level adjustment advisor: per-account guidance for one symbol.
    Reuses FVS regime + book greeks; adds Sentinel's decision matrix on top."""
    symbol = request.args.get("symbol", "SPX").upper()
    mode = request.args.get("mode", "mock")
    try:
        ctx = build_context(symbol, mode)
        reg = S.RegimeView.from_fvs({**ctx.regime, "symbol": symbol},
                                    term_stats(ctx.slices))
        if mode == "live":
            accts = with_ib(list_accounts)
            pos_by = with_ib(lambda ib: {a["account"]:
                             fetch_positions(ib, symbol, a["account"]) for a in accts})
        else:
            accts = MOCK_ACCOUNTS
            mp = _sentinel_mock_positions(ctx.spot)
            pos_by = {a["account"]: mp for a in accts}

        books = []
        for a in accts:
            bg = book_greeks(ctx, pos_by.get(a["account"], []))
            is_inv = (a["account"] in SENTINEL_INVESTING_ACCOUNTS
                      or (mode == "mock" and a is accts[-1]))   # demo: last = SMSF
            books.append(S.BookView.from_fvs(
                a, bg, label=a["account"],
                pool="investing" if is_inv else "trading",
                smsf_eu_cash_block=is_inv and symbol in S.EU_CASH_INDEX))

        cards = S.advise(reg, books)
        log("sentinel", symbol, {"accounts": len(books),
                                 "conflicts": sum(len(c.conflicts) for c in cards)})
        return jsonify({"symbol": symbol, "mode": mode, "spot": ctx.spot,
                        "headline": reg.headline(),
                        "cards": _sentinel_payload(cards)})
    except Exception as e:                       # noqa: BLE001
        return jsonify({"error": str(e)}), 500


@app.post("/api/payoff")
def api_payoff():
    d = request.get_json(force=True)
    spot, today = float(d["spot"]), trading_today()
    q = q_for(d.get("symbol", ""))
    legs = [Leg(cp=l["cp"], strike=float(l["strike"]),
                expiry=date.fromisoformat(l["expiry"]), qty=int(l["qty"]),
                iv=float(l.get("iv") or 0.18)) for l in d["legs"]]
    entry = (float(d["net_mid"]) if d.get("net_mid") is not None
             else struct_value(spot, legs, today, q=q))
    front = min((l.expiry - today).days for l in legs)
    xs, exp, now = [], [], []
    s = spot * 0.90
    while s <= spot * 1.10:
        xs.append(round(s, 1))
        exp.append(round(struct_value(s, legs, today, elapsed=front, q=q) - entry, 2))
        now.append(round(struct_value(s, legs, today, elapsed=min(5, front), q=q) - entry, 2))
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
