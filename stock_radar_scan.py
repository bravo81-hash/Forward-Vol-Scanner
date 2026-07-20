#!/usr/bin/env python3
"""Run the durable after-close stock scan without opening the web app."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from stock_radar import due_cadences, run_scan


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--cadence", choices=("auto", "daily", "weekly"), default="auto")
    p.add_argument("--source", choices=("yf", "mock"), default="yf")
    p.add_argument("--due-only", action="store_true",
                   help="do nothing unless the New York after-close watchlist is due")
    args = p.parse_args()
    ny = datetime.now(ZoneInfo("America/New_York"))
    if args.due_only:
        due = due_cadences(ny, args.source)
        cadences = due if args.cadence == "auto" else [args.cadence] if args.cadence in due else []
    else:
        cadences = (["daily", "weekly"] if args.cadence == "auto" and ny.weekday() == 4
                    else ["daily"] if args.cadence == "auto" else [args.cadence])
    if not cadences:
        print(json.dumps({"status": "not_due", "new_york_time": ny.isoformat()}))
        return
    for cadence in cadences:
        out = run_scan(cadence, args.source)
        print(json.dumps({"snapshot_id": out["snapshot_id"], "session": out["session"],
                          "cadence": cadence,
                          "symbols": [x["symbol"] for x in out["candidates"]]}, indent=2))


if __name__ == "__main__":
    main()
