#!/usr/bin/env python3
"""snapshot.py — P8: one CLI run -> a dated, self-contained HTML report.

Built for the actual workflow: read on the phone before the 15:00-15:40 ET
window instead of babysitting Flask pre-dawn from Melbourne. Runs the same
build_context -> shortlist pipeline as webapp.py, per symbol x per account,
and writes reports/scan_YYYYMMDD_HHMM.html. No server, no JS.

Usage:
    python snapshot.py                      # mock, all SYMBOLS, MOCK_ACCOUNTS
    python snapshot.py --mode live          # live TWS, all real accounts
    python snapshot.py --symbols SPX RUT    # subset
    python snapshot.py --account U1234567   # one account only (live)
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

from core.context import build_context
from core.ib_client import with_ib
from core.reprice import reprice_cards
from core.walls import scan_walls
from portfolio.accounts import MOCK_ACCOUNTS, list_accounts
from portfolio.book import book_greeks, fetch_positions, stress_book
from portfolio.risk import book_warnings
from selection.ranker import shortlist

SYMBOLS = ["SPX", "SPY", "QQQ", "RUT", "IWM"]

CSS = """
body{background:#0B0E14;color:#E7EDF6;font:15px/1.5 -apple-system,'Segoe UI',sans-serif;
     margin:0;padding:20px}
h1{font-size:20px;margin:0 0 4px}
.meta{color:#5C6A82;font-size:12px;margin-bottom:22px}
.sym{border:1px solid #243049;border-radius:10px;margin-bottom:16px;overflow:hidden}
.sym h2{background:#141C2D;margin:0;padding:10px 14px;font-size:15px;display:flex;
        justify-content:space-between}
.acct{padding:12px 14px;border-top:1px solid #1B2333}
.acct .label{font-weight:600;font-size:13px;color:#9AA8C0}
.verdict{display:inline-block;padding:2px 9px;border-radius:6px;font-size:11px;
         letter-spacing:.04em;text-transform:uppercase;font-weight:700}
.v-trade{background:rgba(52,214,160,.15);color:#34D6A0}
.v-caution{background:rgba(242,178,62,.15);color:#F2B23E}
.v-stand,.v-warning{background:rgba(251,116,136,.15);color:#FB7488}
.gates{font-size:11.5px;color:#8A98AF;margin:6px 0}
.card{background:#101725;border:1px solid #1B2333;border-radius:8px;
      padding:9px 12px;margin-top:8px;font-size:13px}
.card .label{color:#E7EDF6;font-weight:600;font-size:13px}
.card .legs{font-family:ui-monospace,monospace;font-size:11.5px;color:#8A98AF;margin-top:3px}
.card .row{display:flex;gap:14px;margin-top:6px;font-size:12px;color:#9AA8C0;flex-wrap:wrap}
.card .row b{color:#E7EDF6}
.stale{color:#F2B23E;font-weight:600}
</style>"""


def render_symbol(symbol: str, out: dict) -> str:
    v = out["verdict"]
    vcls = ("v-warning" if v.upper().startswith("WARNING") else
            "v-stand" if "STAND" in v.upper() else
            "v-caution" if "CAUTION" in v.upper() else "v-trade")
    gates = "".join(f"[{g['code']}{'!' if g['hard'] else ''}] {g['msg']}<br>"
                    for g in out.get("gates", []))
    stale = out.get("data", {}).get("fresh", True) is False
    stale_html = (f'<div class="stale">STALE DATA — {out["data"]["note"]}</div>'
                 if stale else "")
    cards = "".join(_card(c) for c in out.get("cards", []))
    acct = out.get("book", {}).get("account") or "—"
    return (f'<div class="acct"><div class="label">{acct}</div>'
           f'<span class="verdict {vcls}">{v}</span> '
           f'<span style="color:#5C6A82">size {out.get("size","?")}</span>'
           f'{stale_html}<div class="gates">{gates}</div>{cards}</div>')


def _card(c: dict) -> str:
    m = c.get("manage", {})
    lots = c.get("lots", {})
    tr = "".join(f'{t["price"]:g} ({t["side"]}, {t["atr_away"]}atr, '
                f'{t["p_touch_pct"]}% touch) ' for t in m.get("triggers", []))
    return (f'<div class="card"><span class="label">{c["label"]}</span> '
           f'<span style="color:#5C6A82">score {c["score"]}</span>'
           f'<div class="legs">{" / ".join(c["legs"])}</div>'
           f'<div class="row">'
           f'<span><b>lots</b> {lots.get("lots","—")} ({lots.get("binding") or "n/a"})</span>'
           f'<span><b>PT/SL</b> ${m.get("pt_dollars","—")} / ${m.get("sl_dollars","—")}</span>'
           f'<span><b>T+5</b> ${m.get("t5_pnl",{}).get("flat","—")}</span>'
           f'<span><b>exit by</b> {m.get("time_stop",{}).get("exit_by","—")}</span>'
           f'</div><div class="row"><span><b>triggers</b> {tr or "—"}</span></div></div>')


def build_report(symbols: list[str], mode: str, account_filter: str | None) -> str:
    accts = MOCK_ACCOUNTS if mode == "mock" else with_ib(list_accounts)
    if account_filter:
        accts = [a for a in accts if a["account"] == account_filter]

    sections = []
    for symbol in symbols:
        ctx = build_context(symbol, mode)
        acct_html = []
        for a in accts:
            if mode == "live":
                def job(ib, _sym=symbol, _acc=a["account"]):
                    return fetch_positions(ib, _sym, _acc, with_greeks=True)
                try:
                    pos = with_ib(job)
                    ctx.book = book_greeks(ctx, pos)
                    ctx.book["stress"] = stress_book(ctx, pos)
                except Exception as e:                    # noqa: BLE001
                    ctx.book = {"error": str(e)}
            if isinstance(ctx.book, dict):
                ctx.book["account"], ctx.book["nlv"] = a["account"], a.get("nlv")
            out = shortlist(ctx)
            if mode == "live" and out["cards"]:
                try:
                    def enrich(ib):
                        reprice_cards(ib, symbol, ctx.spot, ctx.today, out["cards"])
                        return scan_walls(ib, symbol, ctx, out["cards"])
                    out["walls"] = with_ib(enrich)
                except Exception as e:                     # noqa: BLE001
                    out["enrich_error"] = str(e)
            out["book_warnings"] = book_warnings(ctx.book)
            out["data"] = ctx.data
            acct_html.append(render_symbol(symbol, out))
        sections.append(f'<div class="sym"><h2>{symbol} '
                        f'<span style="color:#5C6A82">spot {ctx.spot:g}</span></h2>'
                        + "".join(acct_html) + "</div>")

    gen = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
           f"<title>Scan {gen}</title><style>{CSS}</head><body>"
           f"<h1>TE Playbook — Snapshot</h1>"
           f"<div class='meta'>Generated {gen} · mode={mode}</div>"
           f"{''.join(sections)}</body></html>")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["mock", "live"], default="mock")
    ap.add_argument("--symbols", nargs="+", default=SYMBOLS)
    ap.add_argument("--account", default=None, help="filter to one account (live)")
    ap.add_argument("--out", default=None, help="output path (default: reports/scan_*.html)")
    args = ap.parse_args()

    html = build_report(args.symbols, args.mode, args.account)
    out_dir = Path(__file__).with_name("reports")
    out_dir.mkdir(exist_ok=True)
    path = Path(args.out) if args.out else out_dir / (
        f"scan_{datetime.now():%Y%m%d_%H%M}.html")
    path.write_text(html)
    print(f"snapshot -> {path}")


if __name__ == "__main__":
    main()
