#!/usr/bin/env python3
"""Weekend Card — the Saturday pipeline for the equity LEAPS engine.

Runs after Friday's US close has settled. Refreshes fundamentals, snapshots
them point-in-time, runs Radar-B, attaches a provisional Gate E structure to
each survivor, renders cards, and writes JSON for the Today screen.

Delivery is PULL-based: this writes a file the app reads. No Telegram, no
push service. Set --email to send yourself a copy via local SMTP if you want
one, but the app view is the primary surface.

The output is a WATCHLIST with armed triggers, not a set of orders. Nothing
should be entered Monday on the strength of a Saturday ranking — the
structure is provisional and is recomputed when the trigger actually fires.

    python weekend_card.py --limit 5 --out data/weekend_card.json
    python weekend_card.py --symbols AAPL,MSFT,NFLX --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

DEFAULT_OUT = Path("data/weekend_card.json")


def run(symbols: list[str] | None = None, limit: int = 5,
        *, snapshot_dir: Path | None = None) -> dict:
    from core.fundamentals import fetch_many, snapshot
    from core.stock_data import earnings_date_yf, histories_yf
    from selection import cards_e, gate_e, radar_b
    from selection.stock_radar import load_universe

    syms = symbols or [u["symbol"] for u in load_universe()]
    print(f"[weekend] universe: {len(syms)} symbols", file=sys.stderr)

    bars = histories_yf(syms, period="2y")
    bench = histories_yf(["SPY"], period="2y").get("SPY")
    print(f"[weekend] histories: {len(bars)} fetched", file=sys.stderr)

    funds = fetch_many(list(bars))
    unrated = [s for s, f in funds.items() if f.status != "OK"]
    if unrated:
        print(f"[weekend] UNRATED and dropped: {', '.join(sorted(unrated)[:20])}",
              file=sys.stderr)
    snapshot(funds, directory=snapshot_dir)

    radar = radar_b.scan(bars, funds, bench_bars=bench, limit=limit)
    print(f"[weekend] qualified {radar['qualified']}, returning {radar['returned']}",
          file=sys.stderr)

    entries = []
    for row in radar["watchlist"]:
        symbol = row["symbol"]
        m = row["metrics"]
        trig = radar_b.trigger(m, bars[symbol],
                               earnings=earnings_date_yf(symbol))

        card, payload = None, None
        try:
            from core.yf_client import build_context_yf
            ctx = build_context_yf(symbol)
            payload = gate_e.build(ctx, "long", trigger_fired=trig["fired"])
            card = cards_e.render(payload,
                                  radar={"reasons": row["reasons"]},
                                  trigger=trig)
        except Exception as exc:                     # noqa: BLE001
            print(f"[weekend] {symbol}: no chain ({type(exc).__name__})",
                  file=sys.stderr)

        entries.append({
            "symbol": symbol,
            "score": row["score"],
            "score_parts": row.get("score_parts", {}),
            "trigger": trig,
            "gate_e": payload,
            "card": card,
            "reasons": row["reasons"],
        })

    return {"generated": date.today().isoformat(),
            "policy_id": radar["policy_id"],
            "qualified": radar["qualified"],
            "returned": radar["returned"],
            "note": ("Provisional structures only. Re-run Gate E when the "
                     "trigger fires, because chain conditions move and the "
                     "IV/RV ratio can cross 1.0 in either direction."),
            "entries": entries}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--symbols", help="comma-separated override universe")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true", help="print, do not write")
    ap.add_argument("--text", action="store_true", help="print rendered cards")
    args = ap.parse_args()

    syms = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
            if args.symbols else None)
    out = run(syms, args.limit)

    if args.text:
        from selection import cards_e
        for e in out["entries"]:
            if e["card"]:
                print(cards_e.to_text(e["card"]))

    if args.dry_run:
        print(json.dumps(out, indent=2, default=str))
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, indent=2, default=str), encoding="utf-8")
    print(f"[weekend] wrote {args.out} ({out['returned']} entries)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
