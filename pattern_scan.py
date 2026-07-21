"""CLI for the Price-Action Scanner module."""
from __future__ import annotations

import argparse
import json

from pattern_scanner.service import run_pattern_scan


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank daily chart-pattern candidates")
    parser.add_argument("--source", choices=("yf", "mock"), default="yf")
    parser.add_argument("--tickers", nargs="*")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--include-forming", action="store_true")
    parser.add_argument("--final-limit", type=int, default=10)
    parser.add_argument("--no-earnings", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run_pattern_scan(source=args.source, tickers=args.tickers,
                              universe_limit=args.limit, live=args.live,
                              include_forming=args.include_forming,
                              final_limit=args.final_limit,
                              include_earnings=not args.no_earnings)
    if args.json:
        print(json.dumps(result, indent=2))
        return
    print(f"{result['liquid_symbols']} liquid -> {result['geometry_count']} geometry "
          f"-> {result['context_count']} context -> {len(result['rows'])} final")
    for row in result["rows"]:
        print(f"#{row['rank']} {row['ticker']:6} {row['pattern']:<26} "
              f"{(row.get('live_status') or row['status']):<20} score {row['score']:.3f} "
              f"trigger {row['trigger']:.2f} invalid {row['invalidation']:.2f}")


if __name__ == "__main__":
    main()
