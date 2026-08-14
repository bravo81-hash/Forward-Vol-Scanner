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
MIN_CANDIDATES = 8          # fewer strikes than this is not a solve
IV_FLOOR, IV_CEIL = 0.02, 3.0
DEGRADED_FRAC = 0.10        # net extrinsic above 10% of ATM is not a ZEBRA


def curve_iv(ctx, slc, strike: float) -> float:
    """Real solved IV at a strike, falling back to the 3-anchor interpolation.

    iv_at() interpolates between ATM and the 25-delta wings, which is fine for
    forward-vol work near those points and poor deep ITM - exactly where the
    ZEBRA long leg sits. When equity_context captured the solved chain curve,
    use it.
    """
    curve = (ctx.data or {}).get("iv_curve", {}).get(slc.expiry.isoformat())
    if curve:
        exact = curve.get(strike)
        if exact:
            return _clamp(exact)
        near = min(curve, key=lambda k: abs(k - strike))
        if abs(near - strike) <= max(1.0, strike * 0.02):
            return _clamp(curve[near])
    # iv_at extrapolates up to 1.6x beyond the 25-delta wings, which deep ITM
    # can reach zero or go negative. core.pricing.bs_greeks then short-circuits
    # to a degenerate delta of exactly 1.00 - which is how a strike carrying
    # $17.78 of time value reported 100 delta on NVDA.
    return _clamp(iv_at(slc, strike))


def _clamp(iv: float) -> float:
    return min(max(float(iv), IV_FLOOR), IV_CEIL)


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
    target = extrinsic(ctx.spot, atm_strike, t, curve_iv(ctx, slc, atm_strike), ctx.q)

    candidates = [k for k in ctx.strikes if k < ctx.spot] or [atm_strike]
    best, best_err, best_ex, table = candidates[0], None, 0.0, []
    for k in sorted(candidates, reverse=True):
        ex = extrinsic(ctx.spot, k, t, curve_iv(ctx, slc, k), ctx.q)
        err = abs(2 * ex - target)
        table.append({"strike": k, "extrinsic": round(ex, 3),
                      "net_extrinsic": round(2 * ex - target, 3)})
        if best_err is None or err < best_err:
            best, best_err, best_ex = k, err, ex

    # A solve over a handful of strikes is not a solve. Report the grid so the
    # card can say so rather than presenting the only available strike as the
    # chosen one.
    return best, {"atm_extrinsic": round(target, 3),
                  "long_extrinsic": round(best_ex, 3),
                  "net_extrinsic": round(2 * best_ex - target, 3),
                  "abs_error": round(best_err, 3),
                  "candidates": len(candidates),
                  "thin_grid": len(candidates) < MIN_CANDIDATES,
                  "degraded": best_err > DEGRADED_FRAC * target,
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

        # Strategy.leg() stamps iv_at() onto the Leg, and every downstream
        # price, greek and breakeven is computed from that. Deep ITM it is the
        # wrong number, so the legs are built with the solved curve instead.
        legs = [Leg(cp="C", strike=long_k, expiry=slc.expiry, qty=+2,
                    iv=curve_iv(ctx, slc, long_k)),
                Leg(cp="C", strike=atm, expiry=slc.expiry, qty=-1,
                    iv=curve_iv(ctx, slc, atm))]
        if min(l.iv for l in legs) <= IV_FLOOR:
            return []
        s = self.make(ctx, f"ZEBRA {long_k:g}/{atm:g}C {slc.dte}d", legs,
                      score=0.5, rationale=[], gamma_test=False)

        t = max(slc.dte, 1) / 365.0
        d_long = bs_greeks(ctx.spot, long_k, t, curve_iv(ctx, slc, long_k),
                           "C", q=ctx.q)["delta"]
        d_atm = bs_greeks(ctx.spot, atm, t, curve_iv(ctx, slc, atm),
                          "C", q=ctx.q)["delta"]
        net_delta = 2 * d_long - d_atm
        # Breakeven comes from struct_metrics, which walks the actual payoff.
        # The closed form 2K_long - K_short + debit is only valid ABOVE the
        # short strike, and silently returns a wrong number when the true
        # breakeven falls between the strikes.
        breakeven = s.breakevens[0] if s.breakevens else None

        solve_note = (
            f"Long strike {long_k:g} carries ${diag['long_extrinsic']:.2f} of time value "
            f"against ${diag['atm_extrinsic']:.2f} at the money, leaving net extrinsic of "
            f"${diag['net_extrinsic']:+.2f} per spread.")
        if diag["degraded"]:
            solve_note += (
                f" That is more than {DEGRADED_FRAC * 100:.0f}% of the ATM time value, so this "
                f"is a back ratio carrying real premium rather than a true "
                f"zero-extrinsic ZEBRA.")

        s.rationale = [
            solve_note,
            f"Net delta computed from the live chain is {net_delta * 100:.0f}, with the long "
            f"leg at {d_long * 100:.0f} delta and the short leg at {d_atm * 100:.0f} delta; at "
            f"{slc.dte} days the ATM delta sits well above 50, which is the effect that "
            f"quietly erodes the 100-delta property.",
            "Theta and vega are close to zero when the solve is clean, so this is "
            "stock replacement rather than a premium position, and it needs "
            "direction rather than time to pay.",
        ]
        if diag["degraded"] and diag["scan"]:
            near = sorted(diag["scan"], key=lambda r: abs(r["net_extrinsic"]))[:3]
            s.rationale.append(
                "Closest strikes the solver could reach were "
                + ", ".join("%g (net %+.2f)" % (r["strike"], r["net_extrinsic"])
                            for r in near)
                + ", so no listed strike lands near zero extrinsic at this tenor.")
        if diag["thin_grid"]:
            s.rationale.append(
                f"Only {diag['candidates']} listed strikes sat below spot, so the strike "
                f"was picked from a thin grid rather than genuinely solved, and the "
                f"result should be re-checked against the live chain in TWS.")
        s.manage = {
            "roll": f"Roll out before the position drops under 90 days to expiry.",
            "stop": "Close the whole structure on a decisive break of the entry thesis; "
                    "do not keep the long legs to give it room.",
        }
        s.evidence["solver"] = diag
        return [s]
