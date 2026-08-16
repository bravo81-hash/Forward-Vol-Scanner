"""Stock Opportunity Radar API + v3 defaults."""
from __future__ import annotations

from datetime import date

from flask import Blueprint, jsonify, request, send_from_directory

from web import shared
from web.shared import trading_clock

bp = Blueprint("stocks", __name__)


@bp.get("/api/v3/defaults")
def api_v3_defaults():
    """Trading-session defaults use New York, not the browser's local date."""
    clock = trading_clock()
    return jsonify({"entry_date": clock["ny_date"], "entry_time": "15:30",
                    "timezone": "America/New_York", "clock": clock})



@bp.get("/api/stocks/latest")
def api_stocks_latest():
    from stock_radar import latest_watchlist
    cadence = request.args.get("cadence", "daily").lower()
    if cadence not in ("daily", "weekly"):
        return jsonify({"error": "cadence must be daily or weekly"}), 400
    try:
        return jsonify(latest_watchlist(cadence))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 404


@bp.post("/api/stocks/scan")
def api_stocks_scan():
    from stock_radar import run_scan
    data = request.get_json(silent=True) or {}
    cadence = str(data.get("cadence", "daily")).lower()
    source = str(data.get("source", "yf")).lower()
    limit = data.get("limit")
    if cadence not in ("daily", "weekly"):
        return jsonify({"error": "cadence must be daily or weekly"}), 400
    if source not in ("yf", "mock"):
        return jsonify({"error": "source must be yf or mock"}), 400
    try:
        return jsonify(run_scan(cadence, source, limit))
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                     # noqa: BLE001
        return jsonify({"error": f"stock scan failed: {exc}"}), 500


@bp.get("/api/stocks/monitor")
def api_stocks_monitor():
    from stock_radar import monitor
    cadence = request.args.get("cadence", "daily").lower()
    mode = request.args.get("mode", "auto").lower()
    account = request.args.get("account") or None
    nlv = request.args.get("nlv", type=float)
    if cadence not in ("daily", "weekly"):
        return jsonify({"error": "cadence must be daily or weekly"}), 400
    if mode not in ("auto", "live", "yf", "mock"):
        return jsonify({"error": "mode must be auto, live, yf, or mock"}), 400
    try:
        return jsonify(monitor(cadence, mode, account, nlv))
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:                     # noqa: BLE001
        return jsonify({"error": f"stock monitor failed: {exc}"}), 500


@bp.post("/api/stocks/stage")
def api_stocks_stage():
    from stock_radar import stage
    data = request.get_json(force=True)
    try:
        return jsonify(stage(data["candidate_id"], int(data.get("quantity", 1))))
    except (KeyError, ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400


@bp.get("/api/stocks/evidence")
def api_stocks_evidence():
    from stock_radar import evidence
    try:
        return jsonify(evidence(refresh=request.args.get("refresh", "0") == "1"))
    except (ValueError, RuntimeError) as exc:
        return jsonify({"error": str(exc)}), 400

