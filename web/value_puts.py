"""Value Entry Put Scanner API."""
from __future__ import annotations

from flask import Blueprint, current_app, jsonify, request

from web import shared

bp = Blueprint("value_puts", __name__)


def tws_available() -> bool:
    """Same Codespaces guard as the pattern scanner: TWS is not reachable
    from a cloud workspace."""
    from web.patterns import _tws_available
    return _tws_available()


@bp.post("/api/value-puts/discover")
def api_value_put_discover():
    """Rank the maintained stock universe before requesting option chains."""
    from value_put.discovery import discover_value_universe

    data = request.get_json(silent=True) or {}
    try:
        result = discover_value_universe(
            source=str(data.get("source") or "mock").lower(),
            limit=int(data.get("limit", 25)),
            min_market_cap=float(data.get("min_market_cap", 5_000_000_000)),
            min_average_dollar_volume=float(
                data.get("min_average_dollar_volume", 50_000_000)
            ),
            min_quality=float(data.get("min_quality", 68)),
            max_leverage=float(data.get("max_leverage", 3.0)),
            max_price_to_fcf=float(data.get("max_price_to_fcf", 35.0)),
        )
        return jsonify(result)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("value-entry universe discovery failed")
        return jsonify({"error": str(exc)}), 502


@bp.post("/api/value-puts/scan")
def api_value_put_scan():
    """Valuation-first put scan; broker buying power is never treated as risk."""
    from value_put.service import scan_value_puts

    data = request.get_json(silent=True) or {}
    raw_symbols = data.get("symbols")
    if isinstance(raw_symbols, str):
        raw_symbols = raw_symbols.split(",")
    try:
        result = scan_value_puts(
            symbols=raw_symbols,
            source=str(data.get("source") or "mock").lower(),
            mode=str(data.get("mode") or "cash_secured").lower(),
            overrides=data.get("overrides") or {},
            hurdle_rate=float(data.get("hurdle_rate", .08)),
            nlv=float(data.get("nlv", 100_000)),
            available_cash=float(data.get("available_cash", 50_000)),
            sector_limit_pct=float(data.get("sector_limit_pct", .20)),
            min_dte=int(data.get("min_dte", 45)),
            max_dte=int(data.get("max_dte", 390)),
        )
        return jsonify(result)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("value-entry put scan failed")
        return jsonify({"error": str(exc)}), 502


@bp.post("/api/value-puts/validate-tws")
def api_value_put_validate_tws():
    """Refresh one exact finalist and obtain what-if margin; never stage it."""
    from value_put.tws import validate_candidate_tws

    if not tws_available():
        return jsonify({"error":
                        "TWS validation is unavailable in Codespaces. Run this app on the same computer as TWS."}), 409
    data = request.get_json(silent=True) or {}
    symbol = str(data.get("symbol") or "").upper().strip()
    candidate = data.get("candidate") or {}
    if not symbol or not candidate.get("expiry") or candidate.get("strike") is None:
        return jsonify({"error": "symbol, candidate expiry and strike are required"}), 400
    try:
        result = shared.with_ib(lambda ib: validate_candidate_tws(
            ib, symbol, candidate, account=data.get("account")))
        return jsonify(result)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("value-put TWS validation failed")
        return jsonify({"error": str(exc)}), 502

