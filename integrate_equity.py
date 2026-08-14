#!/usr/bin/env python3
"""Idempotent integration of the equity LEAPS engine into pre-existing files.

Context diffs against files that already live in the repo proved fragile —
the Codespace tree differs from the public default branch, so hunks failed on
line context. This script does the same edits by string match instead, checks
whether each is already present, and reports what it did.

Safe to run repeatedly. Touches only:
    core/yf_client.py   — dte_range override so LEAPS expiries are reachable
    webapp.py           — /equity page route + Gate E route uses equity_context
    static/*.html       — nav link to the new page

Run from the repo root:   python integrate_equity.py
Check without writing:    python integrate_equity.py --check
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CHANGES: list[str] = []
SKIPPED: list[str] = []
FAILED: list[str] = []


def edit(path: str, marker: str, fn) -> None:
    """Apply fn(text)->text|None unless `marker` already present."""
    p = Path(path)
    if not p.exists():
        FAILED.append(f"{path}: file not found")
        return
    text = p.read_text(encoding="utf-8")
    if marker in text:
        SKIPPED.append(f"{path}: already integrated")
        return
    out = fn(text)
    if out is None or out == text:
        FAILED.append(f"{path}: anchor not found — integrate by hand")
        return
    if not ARGS.check:
        p.write_text(out, encoding="utf-8")
    CHANGES.append(path)


# ------------------------------------------------- core/yf_client.py ------
def yf_client(t: str) -> str | None:
    old_sig = "def build_context_yf(symbol: str, today: date | None = None) -> Context:"
    if old_sig not in t:
        return None
    t = t.replace(old_sig, '''def build_context_yf(symbol: str, today: date | None = None,
                     dte_range: tuple[int, int] | None = None) -> Context:
    """Build a Context from yfinance.

    dte_range overrides SCAN_DTE. The default (5, 85) is right for index
    premium work but excludes every LEAPS expiry, so single-name months-long
    structures must pass a wider window explicitly.
    """''', 1)

    anchor = "    fetch = PROXY.get(symbol, symbol)"
    if anchor in t:
        t = t.replace(anchor, "    window = dte_range or SCAN_DTE\n" + anchor, 1)
    else:
        return None

    t = t.replace("if not SCAN_DTE[0] <= dte <= SCAN_DTE[1]:",
                  "if not window[0] <= dte <= window[1]:")
    t = t.replace('f"({len(slices)} expiries in {SCAN_DTE} DTE)")',
                  'f"({len(slices)} expiries in {window} DTE)")')
    t = t.replace("        if len(slices) >= MAX_EXPIRIES:\n            break",
                  "        if len(slices) >= (MAX_EXPIRIES if window == SCAN_DTE\n"
                  "                           else MAX_EXPIRIES * 2):\n            break")
    return t


# ------------------------------------------------------- webapp.py -------
GATE_E_ROUTE = '''@app.get("/api/equity/gate-e")
def api_equity_gate_e():
    """Gate E: single-name equity structure selection.

    symbol: any US equity ticker
    hold:   short (1-2w) | medium (4-6w) | long (months, LEAPS)
    trigger: 1 to declare the reclaim trigger already fired
    """
    from selection import cards_e, gate_e
    from selection.equity_context import build as build_equity

    symbol = request.args.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    hold = request.args.get("hold", "medium").lower()
    if hold not in gate_e.HOLDS:
        return jsonify({"error": f"bad hold '{hold}'"}), 400
    fired = request.args.get("trigger", "0") in ("1", "true", "yes")

    errors, ctx, bars = [], None, []
    try:
        ctx, bars = build_equity(symbol, hold)
    except Exception as e:                           # noqa: BLE001
        errors.append(f"yf: {type(e).__name__}: {e}")
    if ctx is None:
        return jsonify({"error": "no usable chain for this hold",
                        "errors": errors}), 502

    payload = gate_e.build(ctx, hold, trigger_fired=fired, bars=bars)
    payload["card"] = cards_e.render(payload)
    payload["errors"] = errors
    return jsonify(payload)


@app.get("/api/equity/radar")
def api_equity_radar():
    """Radar-B watchlist. NOT signals — entries fire on the separate trigger."""
    from core.fundamentals import fetch_many
    from core.stock_data import histories_yf
    from selection import radar_b
    from selection.stock_radar import load_universe

    limit = int(request.args.get("limit", radar_b.OUTPUT_LIMIT))
    syms = [u["symbol"] for u in load_universe()]
    only = request.args.get("symbols")
    if only:
        want = {s.strip().upper() for s in only.split(",") if s.strip()}
        syms = [s for s in syms if s in want] or sorted(want)

    bars = histories_yf(syms, period="2y")
    bench = histories_yf(["SPY"], period="2y").get("SPY")
    funds = fetch_many([s for s in syms if s in bars])
    out = radar_b.scan(bars, funds, bench_bars=bench, limit=limit)
    out["watchlist"] = [{
        "symbol": r["symbol"], "score": r["score"],
        "score_parts": r.get("score_parts", {}), "reasons": r["reasons"],
        "metrics": {k: v for k, v in r["metrics"].__dict__.items()
                    if not k.startswith("_") and k != "blocks"},
    } for r in out["watchlist"]]
    return jsonify(out)


@app.get("/equity", strict_slashes=False)
def page_equity():
    return send_from_directory("static", "equity_leaps.html")


'''


def webapp(t: str) -> str | None:
    # Remove any earlier version of the routes so this is a clean replace.
    t = re.sub(r'@app\.get\("/api/equity/gate-e"\).*?(?=@app\.get\("/api/smsf"\))',
               "", t, flags=re.S)
    for anchor in ('@app.get("/api/smsf")', '@app.get("/api/status")',
                   '@app.get("/research")'):
        if anchor in t:
            return t.replace(anchor, GATE_E_ROUTE + anchor, 1)
    return None


# ---------------------------------------------------------- nav links ----
NAV = '<a href="/equity">Equity LEAPS</a>'


def nav(t: str) -> str | None:
    for pat in (r'<a href="/stocks"[^>]*>.*?</a>',
                r'<a href="/patterns"[^>]*>.*?</a>',
                r'<a href="/"[^>]*>.*?</a>'):
        m = re.search(pat, t, flags=re.S)
        if m:
            return t[:m.end()] + NAV + t[m.end():]
    return None


def main() -> int:
    edit("core/yf_client.py", "dte_range", yf_client)
    edit("webapp.py", 'send_from_directory("static", "equity_leaps.html")', webapp)
    for page in ("static/last_hour.html", "static/index.html",
                 "static/pattern_scanner.html", "static/stock_radar.html",
                 "static/value_puts.html", "static/campaigns.html"):
        if Path(page).exists():
            edit(page, 'href="/equity"', nav)

    verb = "would change" if ARGS.check else "changed"
    for p in CHANGES:
        print(f"  {verb}: {p}")
    for s in SKIPPED:
        print(f"  skipped: {s}")
    for f in FAILED:
        print(f"  FAILED:  {f}", file=sys.stderr)

    if not CHANGES and not SKIPPED:
        print("\nNothing integrated. Are you in the repo root?", file=sys.stderr)
        return 1
    print(f"\n{len(CHANGES)} {verb}, {len(SKIPPED)} already done, {len(FAILED)} failed.")
    return 1 if FAILED else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report, do not write")
    ARGS = ap.parse_args()
    raise SystemExit(main())