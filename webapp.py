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
from store.campaigns import campaign_store

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


@app.get("/campaigns")
def campaigns_page():
    return send_from_directory("static", "campaigns.html")


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


def _v3_context(symbol: str, mode: str, account: str | None, nlv: float | None):
    """Build a v3 context with one central mandate and optional live book."""
    from config.loader import account_profile
    from core.chain import MOCK, SURFACE_CFG
    from core.yf_client import build_context_yf

    profile = account_profile(account, nlv)
    errors, ctx = [], None
    order = {"mock": ["mock"], "live": ["live"], "yf": ["yf"],
             "auto": (["live", "yf", "mock"] if symbol in SURFACE_CFG else ["yf"])}.get(mode)
    if order is None:
        raise ValueError(f"bad mode '{mode}'")
    for source in order:
        if source == "mock" and symbol not in MOCK:
            continue
        if source == "live" and symbol not in SURFACE_CFG:
            continue
        try:
            ctx = build_context_yf(symbol) if source == "yf" else build_context(symbol, source)
            break
        except Exception as exc:                 # noqa: BLE001
            errors.append(f"{source}: {exc}")
    if ctx is None:
        raise RuntimeError("; ".join(errors) or "no data source")
    ctx.mandate = profile
    if ctx.mode == "live":
        try:
            pos = with_ib(lambda ib: fetch_positions(ib, symbol, account, with_greeks=True))
            ctx.book = book_greeks(ctx, pos)
            ctx.book["stress"] = stress_book(ctx, pos)
        except Exception as exc:                 # book optional for scan, explicit in output
            ctx.book = {"error": str(exc)}
    if isinstance(ctx.book, dict):
        ctx.book.update(account=account, nlv=profile["nlv"], symbol=symbol)
    return ctx, profile, errors


@app.get("/api/v3/opportunities")
def api_v3_opportunities():
    """Executable Gate S candidates for mock/ONE testing and later paper use."""
    from execution.candidates import persist_cards
    from selection.unified import campaign_shortlist
    from selection.lab import strategy_lab

    symbol = request.args.get("symbol", "SPX").upper().strip()
    intent = request.args.get("intent", "auto").lower()
    mode = request.args.get("mode", "mock").lower()
    account = request.args.get("account") or "MOCK-B"
    nlv = request.args.get("nlv", type=float)
    lab = request.args.get("lab", "false").lower() in ("1", "true", "yes")
    if intent not in ("auto", "bull", "neutral", "bear"):
        return jsonify({"error": "intent must be auto, bull, neutral, or bear"}), 400
    try:
        ctx, profile, errors = _v3_context(symbol, mode, account, nlv)
        out = (strategy_lab(ctx, intent, account, profile["nlv"]) if lab
               else campaign_shortlist(ctx, intent, account, profile["nlv"]))
        out["symbol"], out["spot"], out["book"] = symbol, ctx.spot, ctx.book
        if errors:
            out["fallback_chain"] = errors
        persist_cards(out, ttl_seconds=86400 if ctx.mode == "mock" else 900)
        store = campaign_store()
        store.save_snapshot(symbol, account, ctx.mode,
                            bool(ctx.data.get("fresh")),
                            {"data": ctx.data, "regime": ctx.regime,
                             "events": ctx.events, "action": out["action"],
                             "candidate_ids": [c["candidate_id"] for c in out["cards"]]})
        log("v3_opportunities", symbol, {"account": account, "intent": intent,
                                         "cards": len(out["cards"])})
        return jsonify(out)
    except Exception as exc:                     # noqa: BLE001
        return jsonify({"error": str(exc)}), 500


@app.get("/api/v3/candidates/<candidate_id>")
def api_v3_candidate(candidate_id):
    row = campaign_store().candidate(candidate_id)
    return (jsonify(row), 200) if row else (jsonify({"error": "not found"}), 404)


@app.route("/api/v3/campaigns", methods=["GET", "POST"])
def api_v3_campaigns():
    store = campaign_store()
    if request.method == "GET":
        return jsonify(store.campaigns(request.args.get("state")))
    data = request.get_json(force=True)
    try:
        row = store.create_campaign(data["candidate_id"], data.get("quantity", 1),
                                    data.get("test_mode", "optionnet"))
        return jsonify(row), 201
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/v3/campaigns/<campaign_id>")
def api_v3_campaign(campaign_id):
    row = campaign_store().campaign(campaign_id)
    return (jsonify(row), 200) if row else (jsonify({"error": "not found"}), 404)


@app.post("/api/v3/campaigns/<campaign_id>/transition")
def api_v3_transition(campaign_id):
    data = request.get_json(force=True)
    try:
        return jsonify(campaign_store().transition(campaign_id, data["state"],
                                                    data.get("kind", "manual_transition"),
                                                    data.get("payload", {})))
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/v3/campaigns/<campaign_id>/manual-test")
def api_v3_manual_test(campaign_id):
    try:
        return jsonify(campaign_store().add_manual_test(campaign_id,
                                                        request.get_json(force=True)))
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/v3/campaigns/<campaign_id>/orders", methods=["GET", "POST"])
def api_v3_campaign_orders(campaign_id):
    store = campaign_store()
    if request.method == "GET":
        return jsonify(store.campaign_orders(campaign_id))
    data = request.get_json(force=True)
    try:
        return jsonify(store.record_order(data["candidate_id"], data.get("quantity", 1),
                                          data.get("result", {"status": "PaperStaged"}),
                                          campaign_id)), 201
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/v3/orders/<order_id>/fills")
def api_v3_fill(order_id):
    data = request.get_json(force=True)
    try:
        return jsonify(campaign_store().record_fill(order_id, data["quantity"], data["price"],
                                                     data.get("commission", 0), data.get("payload")))
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@app.post("/api/v3/reconcile")
def api_v3_reconcile():
    from campaign.grouping import reconcile_positions
    data = request.get_json(force=True)
    account = data.get("account")
    campaigns = [c for c in campaign_store().campaigns()
                 if not account or c.get("account") == account]
    return jsonify(reconcile_positions(campaigns, data.get("positions", [])))


@app.post("/api/v3/campaigns/<campaign_id>/manage")
def api_v3_manage(campaign_id):
    from management.engine import advise_campaign
    store = campaign_store()
    campaign = store.campaign(campaign_id)
    if not campaign:
        return jsonify({"error": "not found"}), 404
    data = request.get_json(force=True)
    advice = advise_campaign(campaign, data.get("mark", {}), data.get("context", {}))
    store.add_event(campaign_id, "management_advice", advice)
    return jsonify(advice)


@app.get("/api/v3/evidence")
def api_v3_evidence():
    from validation.evidence import evidence_report
    return jsonify(evidence_report())


@app.get("/api/v3/replay")
def api_v3_replay():
    from validation.replay import replay_summary
    return jsonify(replay_summary(symbol=request.args.get("symbol")))


@app.get("/api/v3/portfolio")
def api_v3_portfolio():
    """Aggregate campaign Greeks for the selected test/paper account."""
    from portfolio.governor import aggregate_books
    account = request.args.get("account")
    campaigns = [c for c in campaign_store().campaigns()
                 if (not account or c.get("account") == account)
                 and c.get("state") not in ("CLOSED", "REJECTED")]
    books = []
    for c in campaigns:
        qty, g = c["quantity"], c["card"].get("greeks", {})
        books.append({"symbol": c["symbol"], "nlv": c["card"].get("governor", {}).get("nlv"),
                      "greeks": {k: float(g.get(k, 0)) * qty
                                  for k in ("delta", "gamma", "theta", "vega")}})
    out = aggregate_books(books)
    out.update(account=account, campaigns=len(campaigns), source="campaign ledger")
    return jsonify(out)


@app.post("/api/v3/stage")
def api_v3_stage():
    """Stage only a fresh server-stored candidate; never trusts client legs."""
    from execution.candidates import validate_for_stage
    from portfolio.governor import evaluate_candidate

    data = request.get_json(force=True)
    try:
        checked = validate_for_stage(data["candidate_id"], data.get("quantity", 1))
        cand, card, qty = checked["candidate"], checked["card"], checked["quantity"]
        if cand["mode"] == "mock":
            log("v3_stage_mock", cand["symbol"], {"candidate_id": cand["id"], "qty": qty})
            result = {"orderId": -1, "status": "MockStaged", "transmit": False,
                      "candidate_id": cand["id"], "legs": card["legs_raw"]}
            if data.get("campaign_id"):
                campaign_store().record_order(cand["id"], qty, result, data["campaign_id"])
            return jsonify(result)

        # Live path: refresh book, reprice exact stored legs, and re-run signed risk.
        account = cand["account"]
        ctx, profile, _ = _v3_context(cand["symbol"], "live", account, None)
        live_card = dict(card)
        with_ib(lambda ib: reprice_cards(ib, cand["symbol"], ctx.spot, ctx.today, [live_card]))
        gov = evaluate_candidate(live_card, ctx.book, profile["nlv"], ctx.spot,
                                 card.get("governor", {}).get("size", "FULL"))
        if qty > gov["approved_lots"]:
            raise ValueError(f"fresh governor approves {gov['approved_lots']} lots, requested {qty}")
        result = with_ib(lambda ib: stage_suggestion(ib, cand["symbol"], live_card["legs_raw"],
                                                     live_card["net_mid"], qty,
                                                     transmit=False, account=account))
        log("v3_stage", cand["symbol"], {"candidate_id": cand["id"], **result})
        if data.get("campaign_id"):
            campaign_store().record_order(cand["id"], qty, result, data["campaign_id"])
        return jsonify({**result, "candidate_id": cand["id"], "transmit": False})
    except (KeyError, ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


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
    legs = [Leg(cp=leg["cp"], strike=float(leg["strike"]),
                expiry=date.fromisoformat(leg["expiry"]), qty=int(leg["qty"]),
                iv=float(leg.get("iv") or 0.18)) for leg in d["legs"]]
    entry = (float(d["net_mid"]) if d.get("net_mid") is not None
             else struct_value(spot, legs, today, q=q))
    front = min((leg.expiry - today).days for leg in legs)
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
    symbol = d["symbol"].upper()
    if d.get("mode") == "mock":
        log("stage_mock", symbol, d)
        return jsonify({"orderId": -1, "status": "MockStaged", "margin_change": None,
                        "note": "mock mode — nothing sent to TWS"})
    # The legacy endpoint trusted browser-supplied legs. Live use is disabled;
    # v3 requires a fresh immutable server-side candidate id.
    return jsonify({"error": "legacy live staging disabled; rescan in Campaign v3 and use /api/v3/stage"}), 410


if __name__ == "__main__":
    print("TE Playbook app -> http://127.0.0.1:8765   (mock mode needs no TWS)")
    app.run(host="127.0.0.1", port=8765, debug=False)
