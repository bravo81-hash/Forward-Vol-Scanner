"""Flask blueprints for the FVS web app.

One module per feature area, all registered by `create_app`. Route handlers
resolve their shared dependencies through `web.shared` so there is a single
seam to patch in tests and a single place that touches TWS.
"""
from __future__ import annotations

from flask import Flask


def create_app() -> Flask:
    from web import equity_leaps, last_hour, pages, patterns, research, stocks, v3, value_puts, x4

    app = Flask(__name__, static_folder="../static", static_url_path="/static")
    for module in (pages, research, value_puts, patterns, stocks, last_hour,
                   x4, v3, equity_leaps):
        app.register_blueprint(module.bp)
    return app
