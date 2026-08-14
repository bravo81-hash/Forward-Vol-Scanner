#!/usr/bin/env python3
"""Integrate the equity LEAPS engine into pre-existing FVS files.

Idempotent, string-match based rather than context-diff based, because
whitespace-trimming editors corrupt patches. Safe to run repeatedly.

PART A (as before)
    core/yf_client.py   dte_range override so LEAPS expiries are reachable
    webapp.py           /equity page + API routes
    static/*.html       nav link

PART B (new: IBKR live chains for single-name equities)
    core/chain.py       three hard-coded limits block equity LEAPS entirely:
                          - SURFACE_CFG[symbol] KeyErrors on any ticker
                            outside the six index/ETF entries
                          - SCAN_DTE caps expiries at 85 days
                          - the strike band is +/-20% of spot, too narrow for
                            a ZEBRA long leg, which sits near 0.8x
                        Each becomes an optional parameter. Defaults are
                        unchanged, so index premium work behaves exactly as
                        before.
    core/context.py     threads those parameters through build_context()

Run from the repo root:   python integrate_equity_v2.py
Check without writing:    python integrate_equity_v2.py --check
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
        FAILED.append(f"{path}: anchor not found - integrate by hand")
        return
    if not ARGS.check:
        p.write_text(out, encoding="utf-8")
    CHANGES.append(path)


# ============================ PART A ======================================
def yf_client(t: str) -> str | None:
    old = "def build_context_yf(symbol: str, today: date | None = None) -> Context:"
    if old not in t:
        return None
    t = t.replace(old, '''def build_context_yf(symbol: str, today: date | None = None,
                     dte_range: tuple[int, int] | None = None) -> Context:
    """Build a Context from yfinance.

    dte_range overrides SCAN_DTE. The default (5, 85) is right for index
    premium work but excludes every LEAPS expiry.
    """''', 1)
    anchor = "    fetch = PROXY.get(symbol, symbol)"
    if anchor not in t:
        return None
    t = t.replace(anchor, "    window = dte_range or SCAN_DTE\n" + anchor, 1)
    t = t.replace("if not SCAN_DTE[0] <= dte <= SCAN_DTE[1]:",
                  "if not window[0] <= dte <= window[1]:")
    t = t.replace('f"({len(slices)} expiries in {SCAN_DTE} DTE)")',
                  'f"({len(slices)} expiries in {window} DTE)")')
    t = t.replace("        if len(slices) >= MAX_EXPIRIES:\n            break",
                  "        if len(slices) >= (MAX_EXPIRIES if window == SCAN_DTE\n"
                  "                           else MAX_EXPIRIES * 2):\n            break")
    return t


GATE_E_ROUTE = '''@app.get("/api/equity/gate-e")
def api_equity_gate_e():
    """Gate E: single-name equity structure selection."""
    from selection import cards_e, gate_e
    from selection.equity_context import build as build_equity

    symbol = request.args.get("symbol", "").upper().strip()
    if not symbol:
        return jsonify({"error": "symbol required"}), 400
    hold = request.args.get("hold", "medium").lower()
    if hold not in gate_e.HOLDS:
        return jsonify({"error": f"bad hold '{hold}'"}), 400
    fired = request.args.get("trigger", "0") in ("1", "true", "yes")
    source = request.args.get("source", "auto").lower()

    errors, ctx, bars = [], None, []
    try:
        ctx, bars = build_equity(symbol, hold, source=source)
    except Exception as e:                           # noqa: BLE001
        errors.append(f"{type(e).__name__}: {e}")
    if ctx is None:
        throttled = any("empty response" in e for e in errors)
        return jsonify({
            "error": ("yfinance is rate-limiting - wait about a minute and "
                      "retry; the result is cached once it succeeds"
                      if throttled else "no usable chain for this symbol"),
            "throttled": throttled, "errors": errors}), 502

    payload = gate_e.build(ctx, hold, trigger_fired=fired, bars=bars)
    payload["card"] = cards_e.render(payload)
    payload["source"] = (ctx.data or {}).get("chain_source", "unknown")
    payload["errors"] = errors
    return jsonify(payload)


@app.get("/api/equity/radar")
def api_equity_radar():
    """Radar-B watchlist. NOT signals - entries fire on the separate trigger."""
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
    t = re.sub(r'@app\.get\("/api/equity/gate-e"\).*?(?=@app\.get\("/api/smsf"\))',
               "", t, flags=re.S)
    for anchor in ('@app.get("/api/smsf")', '@app.get("/api/status")'):
        if anchor in t:
            return t.replace(anchor, GATE_E_ROUTE + anchor, 1)
    return None


def registry(t: str) -> str | None:
    """Register the ZEBRA strategy."""
    if "from .debit_spread import DirectionalDebitSpread" not in t:
        return None
    t = t.replace("from .debit_spread import DirectionalDebitSpread",
                  "from .debit_spread import DirectionalDebitSpread\n"
                  "from .zebra import Zebra")
    return t.replace("DirectionalDebitSpread)}", "DirectionalDebitSpread, Zebra)}")


NAV = '<a href="/equity">Equity LEAPS</a>'


def nav(t: str) -> str | None:
    for pat in (r'<a href="/stocks"[^>]*>.*?</a>',
                r'<a href="/patterns"[^>]*>.*?</a>',
                r'<a href="/"[^>]*>.*?</a>'):
        m = re.search(pat, t, flags=re.S)
        if m:
            return t[:m.end()] + NAV + t[m.end():]
    return None


# ============================ PART B ======================================
def chain(t: str) -> str | None:
    """Make build_chain_live usable for arbitrary equities and LEAPS tenors."""
    if "SURFACE_CFG = {" not in t:
        return None

    # 1. dynamic surface config
    helper = '''

def surface_cfg(symbol: str):
    """Contract spec for any US symbol.

    SURFACE_CFG covers the index and ETF names the forward-vol work uses. Any
    other ticker is an ordinary SMART-routed stock whose option tradingClass
    equals its symbol, so an unknown symbol resolves rather than KeyErrors.
    """
    return SURFACE_CFG.get(symbol, ("STK", "SMART", symbol, False))

'''
    m = re.search(r'SURFACE_CFG = \{.*?\n\}\n', t, flags=re.S)
    if not m:
        return None
    t = t[:m.end()] + helper + t[m.end():]

    # 2. optional dte window and strike band
    t = t.replace('''def build_chain_live(ib, symbol: str, today: date, *,
                     fallback_spot: float | None = None,
                     fallback_iv: float | None = None,
                     diagnostics: dict | None = None) -> tuple[float, list[Slice], list[float]]:
    cache_key = (symbol, today)''',
'''def build_chain_live(ib, symbol: str, today: date, *,
                     fallback_spot: float | None = None,
                     fallback_iv: float | None = None,
                     dte_range: tuple[int, int] | None = None,
                     strike_band: tuple[float, float] | None = None,
                     diagnostics: dict | None = None) -> tuple[float, list[Slice], list[float]]:
    # Single-name LEAPS need both a wider tenor window than SCAN_DTE and a
    # wider strike band than +/-20%: a ZEBRA long leg solves to roughly 0.8x
    # spot and the search has to reach past it. Defaults are unchanged.
    window = dte_range or SCAN_DTE
    band = strike_band or (0.8, 1.2)
    cache_key = (symbol, today, window, band)''')

    t = t.replace("    st, exch, tc, is_idx = SURFACE_CFG[symbol]\n"
                  "    und = (Index(symbol, exch, \"USD\") if is_idx else Stock(symbol, \"SMART\", \"USD\"))",
                  "    st, exch, tc, is_idx = surface_cfg(symbol)\n"
                  "    und = (Index(symbol, exch, \"USD\") if is_idx else Stock(symbol, \"SMART\", \"USD\"))")

    t = t.replace("        if SCAN_DTE[0] <= (d - today).days <= SCAN_DTE[1] and d.weekday() == 4:",
                  "        if window[0] <= (d - today).days <= window[1] and d.weekday() == 4:")
    t = t.replace("    strikes = sorted(k for k in chain.strikes if 0.8 * spot < k < 1.2 * spot)",
                  "    strikes = sorted(k for k in chain.strikes\n"
                  "                     if band[0] * spot < k < band[1] * spot)")
    return t


def context(t: str) -> str | None:
    if "from .chain import" not in t:
        return None
    t = t.replace("from .chain import SURFACE_CFG, build_chain_live, build_chain_mock",
                  "from .chain import (SURFACE_CFG, build_chain_live, build_chain_mock,\n"
                  "                    surface_cfg)")
    t = t.replace('''def build_context(symbol: str, mode: str = "mock", today: date | None = None,
                  host=None, port=None, manual: dict | None = None) -> Context:''',
'''def build_context(symbol: str, mode: str = "mock", today: date | None = None,
                  host=None, port=None, manual: dict | None = None,
                  dte_range: tuple[int, int] | None = None,
                  strike_band: tuple[float, float] | None = None) -> Context:''')
    t = t.replace("            st, exch, tc, is_idx = SURFACE_CFG[symbol]",
                  "            st, exch, tc, is_idx = surface_cfg(symbol)")
    t = t.replace('''            sp, sl, ks = build_chain_live(
                ib, symbol, today, fallback_spot=brs[-1][4],
                fallback_iv=fallback_iv, diagnostics=diag)''',
'''            # Passed only when set, so the default index path keeps its
            # original call signature - existing stubs in the test suite bind
            # to it exactly.
            extra = {}
            if dte_range:
                extra["dte_range"] = dte_range
            if strike_band:
                extra["strike_band"] = strike_band
            sp, sl, ks = build_chain_live(
                ib, symbol, today, fallback_spot=brs[-1][4],
                fallback_iv=fallback_iv, diagnostics=diag, **extra)''')
    return t


def main() -> int:
    edit("core/yf_client.py", "dte_range", yf_client)
    edit("webapp.py", 'send_from_directory("static", "equity_leaps.html")', webapp)
    edit("core/chain.py", "def surface_cfg(", chain)
    edit("core/context.py", "surface_cfg(symbol)", context)
    edit("strategies/__init__.py", "from .zebra import Zebra", registry)
    for page in ("static/last_hour.html", "static/index.html",
                 "static/pattern_scanner.html", "static/stock_radar.html",
                 "static/value_puts.html", "static/campaigns.html",
                 "static/research.html"):
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
    ap.add_argument("--check", action="store_true")
    ARGS = ap.parse_args()
    raise SystemExit(main())