"""Static page routes."""
from __future__ import annotations

from flask import Blueprint, jsonify, request, send_from_directory

from web import shared
from web.shared import STATIC_DIR

bp = Blueprint("pages", __name__)


@bp.get("/")
def index():
    """The focused branch opens directly into the last-hour decision desk."""
    return send_from_directory(shared.STATIC_DIR, "last_hour.html")


@bp.get("/research")
def research_page():
    return send_from_directory(shared.STATIC_DIR, "index.html")


@bp.get("/campaigns")
def campaigns_page():
    return send_from_directory(shared.STATIC_DIR, "campaigns.html")


@bp.get("/last-hour")
def last_hour_page():
    return send_from_directory(shared.STATIC_DIR, "last_hour.html")


@bp.get("/stocks")
def stock_radar_page():
    return send_from_directory(shared.STATIC_DIR, "stock_radar.html")


@bp.get("/patterns")
def pattern_scanner_page():
    return send_from_directory(shared.STATIC_DIR, "pattern_scanner.html")


@bp.get("/value-puts", strict_slashes=False)
def value_put_scanner_page():
    return send_from_directory(shared.STATIC_DIR, "value_puts.html")


@bp.get("/x4", strict_slashes=False)
def x4_page():
    return send_from_directory(shared.STATIC_DIR, "x4.html")

