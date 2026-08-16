"""X4 Live Strategy Lab API."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, send_from_directory

from web import shared

bp = Blueprint("x4", __name__)


@bp.get("/api/x4/build")
def api_x4_build():
    """Build inspectable V14/V17/V22 candidates from the current surface."""
    from execution.optionstrat import optionstrat_url
    from selection.x4 import build_x4

    symbol = request.args.get("symbol", "SPX").upper().strip()
    mode = request.args.get("mode", "mock").lower()
    account = request.args.get("account") or ("MOCK-A" if mode == "mock" else None)
    nlv = request.args.get("nlv", type=float)
    setup = request.args.get("setup", "auto").lower()
    iv_state = request.args.get("iv_state", "auto").lower()
    posture = request.args.get("posture", "auto").lower()
    if symbol not in ("SPX", "RUT", "SPY", "QQQ", "IWM"):
        return jsonify({"error": "X4 supports SPX, RUT, SPY, QQQ, or IWM"}), 400
    if mode not in ("live", "mock"):
        return jsonify({"error": "mode must be live or mock"}), 400
    try:
        ctx, profile, errors = shared.v3_context(symbol, mode, account, nlv,
                                           mandate="margin")
        out = build_x4(ctx, setup=setup, iv_state=iv_state, posture=posture)
        out["account"] = profile
        for card in out["cards"]:
            card["optionstrat_url"] = optionstrat_url(symbol, card["legs_raw"])
            card["one_recipe"] = {
                "entry_date": ctx.today.isoformat(),
                "entry_time_et": ctx.data.get("as_of_time", "15:30"),
                "melbourne_date": ctx.data.get("melbourne_date"),
                "melbourne_time": ctx.data.get("melbourne_time"),
                "spot": ctx.spot,
            }
        if ctx.mode == "live":
            try:
                shared.with_ib(lambda ib: shared.reprice_cards(
                    ib, symbol, ctx.spot, ctx.today, out["cards"],
                    strict_option_liquidity=True))
                for card in out["cards"]:
                    if card["strategy"] == "v17":
                        card["upside_plateau"] = round(-card["net_mid"] * 100, 2)
                        if card["upside_plateau"] < 0:
                            card["rationale"].append(
                                "LIVE GATE: upper expiration plateau is below zero; this is not a valid V17 entry.")
                    card["optionstrat_url"] = optionstrat_url(symbol, card["legs_raw"])
                quoted = sum(c.get("mid_src") == "live" for c in out["cards"])
                out["live_capture"] = {
                    "status": "TWS_CONNECTED" if quoted == len(out["cards"]) else "PARTIAL_QUOTES",
                    "quoted_cards": quoted,
                    "captured_at": ctx.data.get("captured_at"),
                }
            except Exception as exc:
                out["live_capture"] = {
                    "status": "LEGS_ONLY", "quote_error": str(exc),
                    "captured_at": ctx.data.get("captured_at"),
                }
        if errors:
            out["fallback_chain"] = errors
        return jsonify(out)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                     # noqa: BLE001
        return jsonify({"error": str(exc)}), 500

