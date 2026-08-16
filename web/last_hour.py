"""Last Hour Trade Desk API."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, send_from_directory

from web import shared

bp = Blueprint("last_hour", __name__)


@bp.get("/api/last-hour/decision")
def api_last_hour_decision():
    """Small live/practice desk: three flies plus TimeEdge and TimeZone only."""
    from execution.candidates import persist_cards
    from portfolio.governor import evaluate_candidate
    from selection.last_hour import PLAYBOOK, _risk_plan, attach_trade_tools, last_hour_decision

    symbol = request.args.get("symbol", "SPX").upper().strip()
    mode = request.args.get("mode", "live").lower()
    account = request.args.get("account") or ("MOCK-A" if mode == "mock" else None)
    mandate = request.args.get("mandate", "margin").lower()
    preferred = request.args.get("preferred", "auto").lower()
    active = request.args.get("active_time_spread", "none").lower()
    trigger = request.args.get("trigger", "none").lower()
    nlv = request.args.get("nlv", type=float)
    te_completed = request.args.get("te_completed", default=0, type=int)
    tz_paper = request.args.get("tz_paper", default=0, type=int)
    allowed = {"auto", *PLAYBOOK.keys()}
    if symbol not in ("SPX", "RUT"):
        return jsonify({"error": "last-hour desk supports SPX or RUT"}), 400
    if mode not in ("live", "mock", "auto"):
        return jsonify({"error": "mode must be live, mock, or auto"}), 400
    if mandate not in ("cash", "margin"):
        return jsonify({"error": "mandate must be cash or margin"}), 400
    if preferred not in allowed:
        return jsonify({"error": "unknown focused strategy"}), 400
    if active not in ("none", "timeedge", "timezone"):
        return jsonify({"error": "active_time_spread must be none, timeedge, or timezone"}), 400
    if trigger not in ("none", "bull_pullback", "bear_failed_bounce"):
        return jsonify({"error": "unknown tape trigger"}), 400
    try:
        ctx, profile, errors = shared.v3_context(symbol, mode, account, nlv,
                                           mandate=mandate)
        out = last_hour_decision(
            ctx, profile, preferred=preferred, active_time_spread=active,
            te_completed=max(te_completed, 0), tz_paper=max(tz_paper, 0),
            trigger=trigger)
        out["inputs"] = out["market"]
        out["test_session_id"] = (
            f"LH-{ctx.today:%Y%m%d}-{str(ctx.data.get('as_of_time', '1530')).replace(':', '')[:4]}"
        )
        for card in out["cards"]:
            card["test_session_id"] = out["test_session_id"]
            card["one_recipe"] = {
                "entry_date": ctx.today.isoformat(),
                "entry_time_et": ctx.data.get("as_of_time", "15:30"),
                "melbourne_date": ctx.data.get("melbourne_date"),
                "melbourne_time": ctx.data.get("melbourne_time"),
                "spot": ctx.spot,
            }
        if ctx.mode == "live" and out["cards"]:
            try:
                shared.with_ib(lambda ib: shared.reprice_cards(ib, symbol, ctx.spot, ctx.today,
                                                  out["cards"]))
                size = "HALF" if symbol == "RUT" or ctx.regime.get("vol_state") == "ELV" else "FULL"
                for card in out["cards"]:
                    attach_trade_tools(card, ctx)
                    card["manage"] = _risk_plan(card["strategy"], card, ctx)
                    gov = evaluate_candidate(card, ctx.book, profile["nlv"], ctx.spot, size)
                    card["governor"] = gov
                    card["lots"] = {"lots": gov["approved_lots"],
                                    "binding": gov["binding"], "size": size}
                    if card["permitted"] and not gov["risk_approved"]:
                        card["permitted"] = card["tws_stage_allowed"] = False
                        card["status"] = "WAIT"
                        card["blocks"].append("portfolio governor approves zero lots")
                primary = next((card for card in out["cards"]
                                if card["permitted"] and
                                (preferred == "auto" or card["strategy"] == preferred)), None)
                out["primary_strategy"] = primary["strategy"] if primary else None
                out["action"] = (f"ENTER {primary['label'].upper()}" if primary else
                                 "NO NEW TRADE — MANAGE RISK / WAIT")
                out["live_capture"] = {
                    "status": "TWS_CONNECTED",
                    "quoted_cards": sum(c.get("mid_src") == "live" for c in out["cards"]),
                    "captured_at": ctx.data.get("captured_at"),
                }
            except Exception as exc:
                out["live_capture"] = {"status": "LEGS_ONLY", "quote_error": str(exc),
                                       "captured_at": ctx.data.get("captured_at")}
        if errors:
            out["fallback_chain"] = errors
        persist_cards(out, ttl_seconds=900 if ctx.mode == "live" else 86400)
        return jsonify(out)
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                     # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

