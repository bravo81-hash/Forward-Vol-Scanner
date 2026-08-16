#!/usr/bin/env python3
"""webapp.py — TE Playbook trade-selection app (browser UI on :8799).

Per ticker: market context -> regime verdict -> top-2 strategy families ->
4 concrete suggestion cards -> stage to TWS (transmit=False) with whatIf
margin. Management stays in OptionNet Explorer by design.

Modes: mock (no TWS, synthetic surface) / live (TWS via ib_insync).
TWS budget per live refresh per symbol: ~1 underlying + 4 x n_expiry option
lines (batched + cancelled, see core/ib_client.py) + 2 cached hist requests.

The routes themselves live in `web/` — one blueprint per feature area. This
file is the entry point: it builds the app and serves it.

Run:  python webapp.py
"""
from __future__ import annotations

import os

from web import create_app
from web.shared import *  # noqa: F401,F403 - kept so existing imports resolve

app = create_app()


def serve(host: str, port: int) -> None:
    """Serve with waitress when it is installed, else the Flask dev server.

    The dev server is single-threaded-by-default and explicitly not for
    anything but development; now that scan state is durable rather than
    process-local, a real WSGI server is a drop-in.
    """
    try:
        from waitress import serve as waitress_serve
    except ImportError:
        print("waitress not installed — using the Flask development server. "
              "`pip install waitress` for the production path.")
        app.run(host=host, port=port, debug=False)
        return
    waitress_serve(app, host=host, port=port, threads=int(os.getenv("FVS_WEB_THREADS", "8")))


if __name__ == "__main__":
    from stock_radar import RadarScheduler, scheduler_enabled
    if scheduler_enabled():
        RadarScheduler().start()
    host = os.getenv("FVS_WEB_HOST", "127.0.0.1")
    port = int(os.getenv("FVS_WEB_PORT", "8799"))
    print(f"TE Playbook app -> http://{host}:{port}   (mock mode needs no TWS)")
    serve(host, port)
