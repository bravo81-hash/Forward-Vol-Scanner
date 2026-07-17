"""Five deliberately small last-hour playbook structures.

The general Campaign Engine keeps its broad research registry.  This module is
the opposite: three regime-specific single-expiry flies plus the canonical
TimeEdge and TimeZone time spreads used by the focused 15:00-15:40 ET desk.
"""
from __future__ import annotations

import math
from statistics import NormalDist

from core.pricing import RISK_FREE, struct_greeks, struct_metrics
from .base import Strategy


def _put_strike(ctx, slc, abs_delta: float) -> float:
    """Approximate a listed put strike for the requested absolute delta."""
    p = min(max(abs_delta, 0.01), 0.95)
    t = max(slc.dte / 365.0, 1 / 365.0)
    iv = max(float(slc.atm_iv), 0.01)
    d1 = NormalDist().inv_cdf(1.0 - p)
    k = ctx.spot * math.exp((RISK_FREE - ctx.q + 0.5 * iv * iv) * t
                            - d1 * iv * math.sqrt(t))
    return ctx.snap(k)


def _slice(ctx, lo: int, hi: int, target: int):
    return Strategy.expiry_in(ctx, lo, hi, target)


class BullishCreditPutBWB(Strategy):
    key, name = "fly_bull", "Bullish credit put BWB"
    evidence_status, policy_id = "ACTIVE", "last-hour-v1"

    def propose(self, ctx):
        slc = (_slice(ctx, 35, 50, 42) if ctx.symbol == "SPX"
               else _slice(ctx, 45, 60, 52))
        if not slc:
            return []
        # The repeated rule in the notes is "shorts ~20 below spot" with a
        # positively-delta broken wing.  SPX keeps the original 40/60 tent;
        # RUT uses a smaller 30/40 tent to stay near +1 to +2 delta/lot.
        body = ctx.snap(ctx.spot - 20.0)
        upper_width, lower_width = ((40.0, 60.0) if ctx.symbol == "SPX"
                                    else (30.0, 40.0))
        upper = ctx.snap(body + upper_width)
        lower = ctx.snap(body - lower_width)
        legs = [self.leg(ctx, slc, "P", upper, +1),
                self.leg(ctx, slc, "P", body, -2),
                self.leg(ctx, slc, "P", lower, +1)]
        s = self.make(ctx, f"Bullish put BWB · {slc.dte} DTE", legs, 1.0,
                      [], gamma_test=False)
        s.rationale = [
            f"Pullback entry: +1 {upper:g}P / -2 {body:g}P / +1 {lower:g}P.",
            f"Starting position delta {s.greeks['delta']:+.1f}/lot; downside wing is {(body-lower)/(upper-body):.2f}x.",
            "Best after a controlled red day with rising IV inside an intact bullish regime.",
        ]
        return [s]


class ChopBalancedPutFly(Strategy):
    key, name = "fly_chop", "Chop near-balanced put fly"
    evidence_status, policy_id = "ACTIVE", "last-hour-v1"

    def propose(self, ctx):
        slc = (_slice(ctx, 14, 21, 16) if ctx.symbol == "SPX"
               else _slice(ctx, 21, 35, 28))
        if not slc:
            return []
        em = self.em(ctx, slc.dte)
        body = ctx.snap(ctx.spot - 0.075 * em)
        frac = 0.30 if ctx.symbol == "SPX" else 0.45
        width = max(frac * em, ctx.spot * 0.005)
        upper = ctx.snap(body + width)
        lower = ctx.snap(body - width)
        legs = [self.leg(ctx, slc, "P", upper, +1),
                self.leg(ctx, slc, "P", body, -2),
                self.leg(ctx, slc, "P", lower, +1)]
        s = self.make(ctx, f"Near-balanced put fly · {slc.dte} DTE", legs, 1.0,
                      [], gamma_test=False)
        s.rationale = [
            f"Body {body:g} sits just below spot; wings {upper-body:g}/{body-lower:g} points.",
            "One cash-defined all-put fly; no entry long call and no calendar overlay.",
            "Use only in a range: flat 8/21 structure, low ADX and paid IV versus forecast RV.",
        ]
        return [s]


class BearishProtectedPutBWB(Strategy):
    key, name = "fly_bear", "Bearish protected debit put BWB"
    evidence_status, policy_id = "ACTIVE", "last-hour-v1"

    def propose(self, ctx):
        slc = (_slice(ctx, 35, 50, 42) if ctx.symbol == "SPX"
               else _slice(ctx, 45, 60, 52))
        if not slc:
            return []
        em = self.em(ctx, slc.dte)
        upper = ctx.snap(ctx.spot - 0.10 * em)
        body = ctx.snap(ctx.spot - 0.42 * em)
        upper_width = max(upper - body, ctx.spot * 0.005)
        lower = ctx.snap(body - 0.82 * upper_width)
        legs = [self.leg(ctx, slc, "P", upper, +1),
                self.leg(ctx, slc, "P", body, -2),
                self.leg(ctx, slc, "P", lower, +1)]
        s = self.make(ctx, f"Bearish protected put BWB · {slc.dte} DTE", legs,
                      1.0, [], gamma_test=False)
        s.rationale = [
            f"Target body {body:g}: +1 {upper:g}P / -2 {body:g}P / +1 {lower:g}P.",
            f"Lower wing is only {(body-lower)/(upper-body):.2f}x the upper wing, so a crash is not the sold tail.",
            "Enter after a failed bounce near resistance, never after an already-extended breakdown candle.",
        ]
        return [s]


class TimeEdgePutCalendar(Strategy):
    key, name = "timeedge", "TimeEdge SPX put calendar"
    evidence_status, policy_id = "ACTIVE", "last-hour-v1"

    def propose(self, ctx):
        if ctx.symbol != "SPX":
            return []
        front = _slice(ctx, 14, 18, 15)
        if not front:
            return []
        backs = [s for s in ctx.slices if 21 <= s.dte <= 28 and s.dte - front.dte >= 7]
        if not backs:
            return []
        back = min(backs, key=lambda s: abs(s.dte - 22))
        strike = _put_strike(ctx, front, 0.35)
        legs = [self.leg(ctx, front, "P", strike, -1),
                self.leg(ctx, back, "P", strike, +1)]
        s = self.make(ctx, f"TimeEdge put calendar · {front.dte}/{back.dte} DTE",
                      legs, 1.0, [], gamma_test=False)
        fiv, biv = legs[0].iv * 100, legs[1].iv * 100
        s.rationale = [
            f"One-sided SPX calendar at the ~35Δ put: sell {front.dte} DTE / buy {back.dte} DTE.",
            f"Exact-strike IV: front {fiv:.2f}v, back {biv:.2f}v, back-front {biv-fiv:+.2f}v.",
            "Standard TimeEdge only: no automatic second tent; one predefined recenter maximum.",
        ]
        return [s]


class TimeZoneHybrid(Strategy):
    key, name = "timezone", "TimeZone RUT PCS + put calendar"
    evidence_status, policy_id = "PAPER", "last-hour-v1"

    def propose(self, ctx):
        if ctx.symbol != "RUT":
            return []
        front = _slice(ctx, 14, 18, 15)
        if not front:
            return []
        backs = [s for s in ctx.slices if 40 <= s.dte <= 47 and s.dte - front.dte >= 21]
        if not backs:
            return []
        back = min(backs, key=lambda s: abs(s.dte - 45))
        pcs_short = _put_strike(ctx, front, 0.14)
        pcs_long = ctx.snap(pcs_short - 20.0)

        # The notes specify an OTM same-strike calendar and a near-flat total
        # entry delta, but not one hard calendar delta.  Search a small frozen
        # delta grid and choose the calendar strike that best achieves that.
        best = None
        for d in (0.20, 0.25, 0.30, 0.35, 0.40):
            k = _put_strike(ctx, front, d)
            legs = [self.leg(ctx, front, "P", pcs_short, -1),
                    self.leg(ctx, front, "P", pcs_long, +1),
                    self.leg(ctx, front, "P", k, -1),
                    self.leg(ctx, back, "P", k, +1)]
            delta = abs(struct_greeks(ctx.spot, legs, ctx.today, q=ctx.q)["delta"])
            if best is None or delta < best[0]:
                best = (delta, k, legs)
        _, cal_strike, legs = best
        s = self.make(ctx, f"TimeZone hybrid · {front.dte}/{back.dte} DTE", legs,
                      1.0, [], gamma_test=False)
        pcs = struct_metrics(ctx.spot, legs[:2], ctx.today, q=ctx.q)
        pcs_credit = max(-pcs["entry"], 0.0)
        s.rationale = [
            f"RUT 20-point PCS: sell {pcs_short:g}P (~14Δ), buy {pcs_long:g}P; model credit {pcs_credit:.2f}.",
            f"OTM put calendar: sell {front.dte} DTE / buy {back.dte} DTE at {cal_strike:g}; combined delta is near flat.",
            "One defense only: reduce the threatened PCS first; the next breach exits the whole position.",
        ]
        s.evidence.update(pcs_credit=round(pcs_credit, 2), calendar_strike=cal_strike)
        return [s]


LAST_HOUR_REGISTRY = {
    s.key: s() for s in (
        BullishCreditPutBWB,
        ChopBalancedPutFly,
        BearishProtectedPutBWB,
        TimeEdgePutCalendar,
        TimeZoneHybrid,
    )
}
