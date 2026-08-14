#!/usr/bin/env python3
"""Diagnose why a symbol will not build an equity LEAPS context.

Every layer is measured and reported separately, because the errors so far
have all been messages about the wrong thing: a rate limit reported as absent
expiries, and absent slices reported as absent expiries. This walks the chain
end to end and says which step actually fails.

    python diag_equity.py NVDA long
    python diag_equity.py NFLX medium
"""
from __future__ import annotations

import sys
import traceback
from datetime import datetime


def main() -> int:
    symbol = (sys.argv[1] if len(sys.argv) > 1 else "NVDA").upper()
    hold = sys.argv[2] if len(sys.argv) > 2 else "long"

    print("=" * 70)
    print("DIAGNOSTIC: %s / hold=%s" % (symbol, hold))
    print("=" * 70)

    # ---- 0. versions -------------------------------------------------
    try:
        import yfinance as yf
        print("yfinance version : %s" % getattr(yf, "__version__", "unknown"))
    except Exception as exc:
        print("FATAL: yfinance import failed: %s" % exc)
        return 1

    from core.events import trading_today
    from selection.equity_context import TARGETS, window_for
    today = trading_today()
    lo, hi = window_for(hold)
    target = TARGETS.get(hold, 45)
    print("today (trading)  : %s" % today)
    print("hold window      : %s-%s DTE, target %s" % (lo, hi, target))
    print("")

    # ---- 1. raw expiry list -----------------------------------------
    print("-" * 70)
    print("STEP 1  tk.options (the raw expiry list)")
    tk = yf.Ticker(symbol)
    try:
        opts = list(tk.options or [])
    except Exception as exc:
        print("  RAISED: %s: %s" % (type(exc).__name__, exc))
        return 1
    print("  returned %d expiries" % len(opts))
    if not opts:
        print("  -> EMPTY. This is rate limiting, not an absence of listings.")
        print("     Wait several minutes and re-run.")
        return 1

    dted = []
    for e in opts:
        try:
            d = datetime.strptime(e, "%Y-%m-%d").date()
        except ValueError:
            continue
        dted.append((e, (d - today).days))
    dted.sort(key=lambda x: x[1])
    print("  DTE range        : %d to %d" % (dted[0][1], dted[-1][1]))
    in_win = [x for x in dted if lo <= x[1] <= hi]
    print("  inside window    : %d  %s" % (len(in_win), [d for _, d in in_win]))
    if not in_win:
        print("  -> No expiry in the hold window. Genuine listings limit.")
        return 1
    print("")

    # ---- 2. per-expiry chain fetch ----------------------------------
    print("-" * 70)
    print("STEP 2  tk.option_chain() per candidate expiry")
    chosen = sorted(sorted(in_win, key=lambda e: abs(e[1] - target))[:3],
                    key=lambda e: e[1])
    print("  bracketing to    : %s" % [d for _, d in chosen])

    from core.pricing import q_for
    from core.yf_client import _slice_from_chain
    spot = None
    try:
        h = tk.history(period="5d", auto_adjust=False)
        spot = float(h["Close"].iloc[-1])
        print("  spot             : %.2f" % spot)
    except Exception as exc:
        print("  spot FAILED      : %s: %s" % (type(exc).__name__, exc))

    ok_slices = 0
    for exp, dte in chosen:
        print("")
        print("  --- expiry %s (%dd) ---" % (exp, dte))
        try:
            ch = tk.option_chain(exp)
        except Exception as exc:
            print("      option_chain RAISED: %s: %s"
                  % (type(exc).__name__, exc))
            print("      -> rate limiting or transient network")
            continue

        ncalls = 0 if ch.calls is None else len(ch.calls)
        nputs = 0 if ch.puts is None else len(ch.puts)
        print("      rows             : %d calls / %d puts" % (ncalls, nputs))
        if not ncalls or not nputs:
            print("      -> EMPTY CHAIN. Rate limiting.")
            continue

        both = sorted(set(ch.calls["strike"]).intersection(set(ch.puts["strike"])))
        print("      shared strikes   : %d" % len(both))
        if both:
            print("      strike span      : %.1f to %.1f" % (both[0], both[-1]))

        ivs = ch.calls.get("impliedVolatility")
        if ivs is not None:
            nz = sum(1 for v in ivs if v and v == v and v > 0)
            print("      call IV non-zero : %d of %d" % (nz, len(ivs)))
            if nz == 0:
                print("      -> ALL IVs ZERO. yfinance returns no IV for this "
                      "expiry; _slice_from_chain cannot build a slice.")

        if spot:
            try:
                slc = _slice_from_chain(
                    datetime.strptime(exp, "%Y-%m-%d").date(), dte, spot,
                    ch.calls, ch.puts, q_for(symbol))
            except Exception as exc:
                print("      _slice_from_chain RAISED: %s: %s"
                      % (type(exc).__name__, exc))
                traceback.print_exc()
                continue
            if slc is None:
                print("      _slice_from_chain -> None")
                atm_k = min(both, key=lambda k: abs(k - spot)) if both else None
                if atm_k is not None:
                    crow = ch.calls[ch.calls["strike"] == atm_k]
                    prow = ch.puts[ch.puts["strike"] == atm_k]
                    civ = float(crow["impliedVolatility"].iloc[0]) if len(crow) else None
                    piv = float(prow["impliedVolatility"].iloc[0]) if len(prow) else None
                    print("      ATM strike %.1f  call IV %s  put IV %s"
                          % (atm_k, civ, piv))
                    print("      -> slice rejected: IV missing or outside "
                          "the 0.02-3.0 sanity band")
            else:
                ok_slices += 1
                print("      slice OK         : ATM %.1f  IV %.1f%%  OI %d  "
                      "spread %.1f%%" % (slc.atm_strike, slc.atm_iv * 100,
                                         slc.oi_atm, slc.atm_spread_pct * 100))

    print("")
    print("-" * 70)
    print("RESULT: %d usable slices of %d attempted (need 2)" % (ok_slices, len(chosen)))
    if ok_slices >= 2:
        print("  Chain is fine. If the app still fails, the problem is above "
              "this layer.")
    elif ok_slices == 0:
        print("  No slices. Read STEP 2 above for the reason per expiry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())