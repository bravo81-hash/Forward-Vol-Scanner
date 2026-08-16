"""Price-Action Pattern Scanner API.

Scan results and background jobs are held in `store.scans` rather than in
process-local dicts. That was the single thing preventing the app from
running more than one worker: a status poll or a TWS overlay that landed on
a different worker could not see the scan, and a restart mid-scan lost it
with no way to recover.
"""
from __future__ import annotations

import threading

from flask import Blueprint, current_app, jsonify, request

from store.scans import scan_store
from web import shared

bp = Blueprint("patterns", __name__)


def _tws_available() -> bool:
    """Codespaces cannot reach TWS on the user's separate workstation."""
    return not shared.truthy_env("CODESPACES") or shared.truthy_env("PATTERN_TWS_ENABLED")


def _scan_options() -> dict:
    raw_tickers = request.args.get("tickers", "")
    tickers = [value.strip().upper() for value in raw_tickers.split(",") if value.strip()]
    return {
        "source": request.args.get("source", "yf").lower(),
        "tickers": tickers or None,
        "universe_limit": request.args.get("limit", type=int),
        "final_limit": request.args.get("final_limit", default=10, type=int),
        "include_forming": request.args.get("include_forming", "0") == "1",
        "live": False,
        "include_earnings": request.args.get("earnings", "1") != "0",
    }


def _run_scan_job(app, job_id: str, options: dict) -> None:
    """Run the long Yahoo scan outside the request."""
    from pattern_scanner.service import run_pattern_scan

    store = scan_store()
    store.update_job(job_id, status="running")
    try:
        out = run_pattern_scan(**options)
        out["scan_id"] = store.save_scan(out)
        store.update_job(job_id, status="complete", result=out)
    except ValueError as exc:
        store.update_job(job_id, status="failed", error=str(exc), status_code=400)
    except Exception as exc:  # noqa: BLE001
        with app.app_context():
            current_app.logger.exception("background pattern scan failed")
        store.update_job(job_id, status="failed", error=str(exc), status_code=502)


def _start_scan_job(options: dict) -> str:
    job_id = scan_store().create_job()
    threading.Thread(
        target=_run_scan_job, args=(current_app._get_current_object(), job_id, options),
        daemon=True, name=f"pattern-scan-{job_id[:8]}",
    ).start()
    return job_id


@bp.get("/api/patterns/scan")
def api_pattern_scan():
    """Synchronous compatibility endpoint; the browser uses background jobs."""
    from pattern_scanner.service import run_pattern_scan

    try:
        out = run_pattern_scan(**_scan_options())
        out["scan_id"] = scan_store().save_scan(out)
        return jsonify(out)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("pattern scan failed")
        return jsonify({"error": str(exc)}), 502


@bp.post("/api/patterns/scan/start")
def api_pattern_scan_start():
    """Return immediately while a long Yahoo scan runs in the background."""
    return jsonify({"job_id": _start_scan_job(_scan_options()), "status": "queued"}), 202


@bp.get("/api/patterns/scan/status/<job_id>")
def api_pattern_scan_status(job_id: str):
    job = scan_store().job(job_id)
    if job is None:
        return jsonify({"error": "The scan job was not found. Run the scan again."}), 404
    if job["status"] == "failed":
        return jsonify({"error": job["error"], "status": "failed"}), job["status_code"]
    if job["status"] == "complete":
        return jsonify(job["result"])
    return jsonify({"job_id": job_id, "status": job["status"]}), 202


@bp.get("/api/patterns/capabilities")
def api_pattern_capabilities():
    available = _tws_available()
    return jsonify({
        "tws_validation": available,
        "tws_reason": (None if available else
                       "TWS validation is unavailable in Codespaces because TWS is not running on the Codespaces server."),
    })


@bp.post("/api/patterns/live")
def api_pattern_live():
    """Overlay TWS quotes on the stored finalists; never repeat the bulk scan."""
    from pattern_scanner.service import validate_pattern_rows

    if not _tws_available():
        return jsonify({"error":
                        "TWS validation is unavailable in Codespaces. Run the Yahoo daily scan here; validate through TWS only when the app is running on the same computer as TWS."}), 409
    data = request.get_json(silent=True) or {}
    scan_id = str(data.get("scan_id") or "")
    cached = scan_store().scan(scan_id)
    if cached is None:
        return jsonify({"error": "The scan expired. Run the daily scan again."}), 409
    try:
        rows, health, excluded = validate_pattern_rows(cached.get("rows") or [])
        cached.update(rows=rows, live_health=health, live_excluded=excluded,
                      scan_id=scan_id)
        return jsonify(cached)
    except Exception as exc:  # noqa: BLE001
        current_app.logger.exception("pattern live validation failed")
        return jsonify({"error": str(exc)}), 502
