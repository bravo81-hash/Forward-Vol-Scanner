"""Campaign Engine v3 API."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, request, send_from_directory

import sentinel as S
from core.events import trading_today
from execution.stage import stage_suggestion
from store.campaigns import campaign_store
from store.log import log
from web import shared

bp = Blueprint("v3", __name__)


@bp.get("/api/v3/historical-snapshot")
def api_v3_historical_snapshot():
    """Build the low-input ONE regime from free daily market history."""
    from core.historical import auto_historical_snapshot

    symbol = request.args.get("symbol", "SPX").upper().strip()
    raw = request.args.get("entry_date", "")
    try:
        as_of = date.fromisoformat(raw)
    except ValueError:
        return jsonify({"error": "choose a valid historical date"}), 400
    if as_of > trading_today():
        return jsonify({"error": "entry date cannot be in the future"}), 400
    if as_of.weekday() > 4:
        return jsonify({"error": "choose a trading weekday"}), 400
    try:
        return jsonify(auto_historical_snapshot(symbol, as_of))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except RuntimeError as exc:
        return jsonify({"error": f"automatic history unavailable: {exc}"}), 503


@bp.get("/api/v3/opportunities")
def api_v3_opportunities():
    """Executable Gate S candidates for mock/ONE testing and later paper use."""
    from execution.candidates import persist_cards
    from selection.lab import strategy_lab
    from selection.unified import campaign_shortlist

    symbol = request.args.get("symbol", "SPX").upper().strip()
    intent = request.args.get("intent", "auto").lower()
    mode = request.args.get("mode", "mock").lower()
    account = request.args.get("account") or "MOCK-B"
    nlv = request.args.get("nlv", type=float)
    mandate = request.args.get("mandate")
    lab = request.args.get("lab", "false").lower() in ("1", "true", "yes")
    if intent not in ("auto", "bull", "neutral", "bear"):
        return jsonify({"error": "intent must be auto, bull, neutral, or bear"}), 400
    if mandate not in (None, "cash", "margin"):
        return jsonify({"error": "mandate must be cash or margin"}), 400
    try:
        as_of, manual = shared.manual_one_context(intent)
        ctx, profile, errors = shared.v3_context(symbol, mode, account, nlv, as_of, manual,
                                           mandate)
        out = (strategy_lab(ctx, intent, account, profile["nlv"]) if lab
               else campaign_shortlist(ctx, intent, account, profile["nlv"]))
        out["symbol"], out["spot"], out["book"] = symbol, ctx.spot, ctx.book
        if ctx.mode == "live" and out["cards"]:
            from portfolio.governor import evaluate_candidate
            try:
                shared.with_ib(lambda ib: shared.reprice_cards(ib, symbol, ctx.spot, ctx.today,
                                                  out["cards"]))
                for card in out["cards"]:
                    gov = evaluate_candidate(card, ctx.book, profile["nlv"], ctx.spot,
                                             out["market_state"]["size"])
                    card["governor"] = gov
                    card["lots"] = {"lots": gov["approved_lots"],
                                    "binding": gov["binding"], "size": gov["size"]}
                out["live_capture"] = {"status": "TWS_CONNECTED",
                                       "quoted_cards": sum(c.get("mid_src") == "live"
                                                           for c in out["cards"]),
                                       "captured_at": ctx.data.get("captured_at")}
            except Exception as exc:             # exact listed legs remain usable in ONE
                out["live_capture"] = {"status": "LEGS_ONLY",
                                       "quote_error": str(exc),
                                       "captured_at": ctx.data.get("captured_at")}
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
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                     # noqa: BLE001
        return jsonify({"error": str(exc)}), 500



@bp.get("/api/v3/candidates/<candidate_id>")
def api_v3_candidate(candidate_id):
    row = campaign_store().candidate(candidate_id)
    return (jsonify(row), 200) if row else (jsonify({"error": "not found"}), 404)


@bp.route("/api/v3/campaigns", methods=["GET", "POST"])
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


@bp.get("/api/v3/campaigns/<campaign_id>")
def api_v3_campaign(campaign_id):
    row = campaign_store().campaign(campaign_id)
    return (jsonify(row), 200) if row else (jsonify({"error": "not found"}), 404)


@bp.post("/api/v3/campaigns/<campaign_id>/transition")
def api_v3_transition(campaign_id):
    data = request.get_json(force=True)
    try:
        return jsonify(campaign_store().transition(campaign_id, data["state"],
                                                    data.get("kind", "manual_transition"),
                                                    data.get("payload", {})))
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@bp.post("/api/v3/campaigns/<campaign_id>/manual-test")
def api_v3_manual_test(campaign_id):
    try:
        return jsonify(campaign_store().add_manual_test(campaign_id,
                                                        request.get_json(force=True)))
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@bp.route("/api/v3/campaigns/<campaign_id>/orders", methods=["GET", "POST"])
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


@bp.post("/api/v3/orders/<order_id>/fills")
def api_v3_fill(order_id):
    data = request.get_json(force=True)
    try:
        return jsonify(campaign_store().record_fill(order_id, data["quantity"], data["price"],
                                                     data.get("commission", 0), data.get("payload")))
    except (KeyError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400


@bp.post("/api/v3/reconcile")
def api_v3_reconcile():
    from campaign.grouping import reconcile_positions
    data = request.get_json(force=True)
    account = data.get("account")
    campaigns = [c for c in campaign_store().campaigns()
                 if not account or c.get("account") == account]
    return jsonify(reconcile_positions(campaigns, data.get("positions", [])))


@bp.post("/api/v3/campaigns/<campaign_id>/manage")
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


@bp.get("/api/v3/evidence")
def api_v3_evidence():
    from validation.evidence import evidence_report
    return jsonify(evidence_report())


@bp.get("/api/v3/replay")
def api_v3_replay():
    from validation.replay import replay_summary
    return jsonify(replay_summary(symbol=request.args.get("symbol")))


@bp.get("/api/v3/portfolio")
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


@bp.post("/api/v3/stage")
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
                      "candidate_id": cand["id"], "legs": card["legs_raw"],
                      "warnings": checked["warnings"]}
            if data.get("campaign_id"):
                campaign_store().record_order(cand["id"], qty, result, data["campaign_id"])
            return jsonify(result)

        # Live path: refresh book, reprice exact stored legs, and re-run signed risk.
        account = cand["account"]
        ctx, profile, _ = shared.v3_context(cand["symbol"], "live", account, None)
        live_card = dict(card)
        shared.with_ib(lambda ib: shared.reprice_cards(ib, cand["symbol"], ctx.spot, ctx.today, [live_card]))
        gov = evaluate_candidate(live_card, ctx.book, profile["nlv"], ctx.spot,
                                 card.get("governor", {}).get("size", "FULL"))
        if qty > gov["approved_lots"]:
            raise ValueError(f"fresh governor approves {gov['approved_lots']} lots, requested {qty}")
        result = shared.with_ib(lambda ib: stage_suggestion(ib, cand["symbol"], live_card["legs_raw"],
                                                     live_card["net_mid"], qty,
                                                     transmit=False, account=account))
        log("v3_stage", cand["symbol"], {"candidate_id": cand["id"], **result})
        if data.get("campaign_id"):
            campaign_store().record_order(cand["id"], qty, result, data["campaign_id"])
        return jsonify({**result, "candidate_id": cand["id"], "transmit": False,
                        "warnings": checked["warnings"]})
    except (KeyError, ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400

