"""ZEBRA — zero extrinsic back ratio, with a NUMERIC strike solver.

The design intent is 2 x ITM calls against 1 x ATM call such that net extrinsic
is zero, giving ~100 delta, ~zero theta and ~zero vega: stock replacement at a
fraction of the capital with defined risk.

The intent breaks silently when strikes are chosen by rule of thumb. Worked
case (NFLX, 305 DTE, 32% IV, spot 78.10):

    70/78 chosen as "70 delta / ATM"  ->  87 delta, $3.15 net extrinsic,
                                          breakeven $81.25 (+4.1%)
    65/78 solved numerically          -> 102 delta, ~$0 net extrinsic,
                                          breakeven $77.85 (-0.3%)

$8 spacing is only 0.35 standard deviations at that tenor, so the long leg is
nowhere near deep enough. Two further effects the nominal approach misses:
at 300+ DTE with r=4.5% the ATM call delta runs ~61 rather than 50, and the
ITM call delta compresses toward 50 as tenor extends.

So: never assume deltas, always solve.
"""
from __future__ import annotations

from core.chain import iv_at
from core.models import Context, Leg, Slice, Suggestion
from core.pricing import bs_greeks, bs_price
from .base import Strategy

MIN_DTE, MAX_DTE, TARGET_DTE = 120, 400, 300


def extrinsic(spot: float, strike: float, t: float, iv: float,
              q: float = 0.0) -> float:
    """Call time value at a strike."""
    return bs_price(spot, strike, t, iv, "C", q=q) - max(0.0, spot - strike)


def solve_long_strike(ctx: Context, slc: Slice, atm_strike: float) -> tuple[float, dict]:
    """Find the listed strike where 2 x extrinsic(K) is closest to extrinsic(ATM).

    Returns (strike, diagnostics). Searches only listed strikes below spot, so
    the answer is always tradable rather than theoretical.
    """
    t = max(slc.dte, 1) / 365.0
    target = extrinsic(ctx.spot, atm_strike, t, iv_at(slc, atm_strike), ctx.q)

    candidates = [k for k in ctx.strikes if k < ctx.spot] or [atm_strike]
    best, best_err, table = candidates[0], None, []
    for k in sorted(candidates, reverse=True):
        ex = extrinsic(ctx.spot, k, t, iv_at(slc, k), ctx.q)
        err = abs(2 * ex - target)
        table.append({"strike": k, "extrinsic": round(ex, 3),
                      "net_extrinsic": round(2 * ex - target, 3)})
        if best_err is None or err < best_err:
            best, best_err = k, err

    return best, {"atm_extrinsic": round(target, 3),
                  "net_extrinsic": round(best_err, 3),
                  "scan": table[:12]}


class Zebra(Strategy):
    key, name = "zebra", "ZEBRA (zero extrinsic back ratio)"
    hypothesis_id, evidence_status = "H020", "HYPOTHESIS"
    policy_id = "gate-e-v1"

    def propose(self, ctx: Context) -> list[Suggestion]:
        slc = self.expiry_in(ctx, MIN_DTE, MAX_DTE, TARGET_DTE)
        if not slc or not ctx.strikes:
            return []

        atm = ctx.snap(ctx.spot)
        long_k, diag = solve_long_strike(ctx, slc, atm)
        if long_k >= atm:
            return []

        legs = [self.leg(ctx, slc, "C", long_k, +2),
                self.leg(ctx, slc, "C", atm, -1)]
        s = self.make(ctx, f"ZEBRA {long_k:g}/{atm:g}C {slc.dte}d", legs,
                      score=0.5, rationale=[], gamma_test=False)

        t = max(slc.dte, 1) / 365.0
        d_long = bs_greeks(ctx.spot, long_k, t, iv_at(slc, long_k), "C", q=ctx.q)["delta"]
        d_atm = bs_greeks(ctx.spot, atm, t, iv_at(slc, atm), "C", q=ctx.q)["delta"]
        net_delta = 2 * d_long - d_atm
        breakeven = long_k * 2 - atm + s.net_mid

        s.rationale = [
            f"Long strike {long_k:g} was solved numerically so that twice its time "
            f"value of ${diag['atm_extrinsic'] / 2:.2f} matches the ATM time value of "
            f"${diag['atm_extrinsic']:.2f}, leaving net extrinsic of "
            f"${diag['net_extrinsic']:.2f} per spread rather than assuming a 70-delta strike.",
            f"Net delta computed from the live chain is {net_delta * 100:.0f}, with the long "
            f"leg at {d_long * 100:.0f} delta and the short leg at {d_atm * 100:.0f} delta; at "
            f"{slc.dte} days the ATM delta sits well above 50, which is the effect that "
            f"quietly erodes the 100-delta property.",
            f"Breakeven at expiry is ${breakeven:.2f} against spot of ${ctx.spot:.2f}, a "
            f"difference of {(breakeven / ctx.spot - 1) * 100:+.1f}%, which is the honest "
            f"measure of how much the structure costs before it starts working.",
            "Theta and vega are close to zero by construction, so this is stock "
            "replacement rather than a premium position, and it needs direction "
            "rather than time to pay.",
        ]
        s.manage = {
            "roll": f"Roll out before the position drops under 90 days to expiry.",
            "stop": "Close the whole structure on a decisive break of the entry thesis; "
                    "do not keep the long legs to give it room.",
        }
        s.evidence["solver"] = diag
        return [s]
