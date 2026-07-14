"""Executable single-expiry fly variants from the Campaign Engine design."""
from __future__ import annotations

from core.models import Context, Suggestion
from .base import Strategy


class BalancedPutFly(Strategy):
    key, name = "balanced_fly", "Balanced ATM put fly"
    hypothesis_id, evidence_status = "H006", "HYPOTHESIS"
    policy_id = "gate-s-v3"
    delta_band = (-0.08, 0.08)

    def propose(self, ctx: Context) -> list[Suggestion]:
        slc = self.expiry_in(ctx, 45, 60, 52) or self.single_expiry(ctx, 35, 65, 50)
        if not slc:
            return []
        body = ctx.snap(ctx.spot)
        em = self.em(ctx, slc.dte)
        out = []
        for frac, label in ((0.45, "standard"), (0.65, "wide")):
            width = max(em * frac, ctx.spot * 0.005)
            lo, hi = ctx.snap(body - width), ctx.snap(body + width)
            legs = [self.leg(ctx, slc, "P", lo, +1),
                    self.leg(ctx, slc, "P", body, -2),
                    self.leg(ctx, slc, "P", hi, +1)]
            s = self.make(ctx, f"Balanced put fly {slc.dte}d {label}", legs,
                          score=0.8, rationale=[], gamma_test=False)
            rr = ctx.regime.get("term", {}).get("rr25_30d", 0.0)
            s.rationale = [f"ATM body {body:g}; symmetric {body-lo:g}/{hi-body:g} wings",
                           f"All-put fly uses one cash-defined width; rr25 {rr:+.1f}v",
                           "Reference alternative to the iron fly when cash efficiency matters"]
            s.score = round(0.8 + max(0, 2.5 - abs(rr)) * .12 - s.liquidity_pen, 3)
            out.append(s)
        return out


class IronFly(Strategy):
    key, name = "iron_fly", "Iron fly"
    hypothesis_id, evidence_status = "H006", "HYPOTHESIS"
    policy_id = "gate-s-v3"
    delta_band = (-0.08, 0.08)

    def propose(self, ctx: Context) -> list[Suggestion]:
        slc = self.expiry_in(ctx, 30, 45, 38) or self.single_expiry(ctx)
        if not slc:
            return []
        body, em = ctx.snap(ctx.spot), self.em(ctx, slc.dte)
        out = []
        for mult, label in ((1.25, "1.25σ"), (1.50, "1.50σ")):
            lp, lc = ctx.snap(body - em * mult), ctx.snap(body + em * mult)
            legs = [self.leg(ctx, slc, "P", lp, +1),
                    self.leg(ctx, slc, "P", body, -1),
                    self.leg(ctx, slc, "C", body, -1),
                    self.leg(ctx, slc, "C", lc, +1)]
            s = self.make(ctx, f"Iron fly {slc.dte}d {label}", legs,
                          score=0.7, rationale=[])
            credit = max(-s.net_mid, 0.0)
            s.rationale = [f"ATM short straddle {body:g}; wings {lp:g}/{lc:g}",
                           f"Model credit {credit:.2f}; intended for high-IV, flat-skew testing",
                           "Cash account may reserve both credit-spread widths; compare with balanced put fly"]
            high = ctx.regime.get("vol_state") in ("ELV", "STR")
            flat = abs(ctx.regime.get("term", {}).get("rr25_30d", 0.0)) < 3
            s.score = round(0.5 + .5 * high + .35 * flat - s.liquidity_pen, 3)
            out.append(s)
        return out


class WideOtmPutFly(Strategy):
    key, name = "otm_put_fly", "Wide OTM put fly"
    hypothesis_id, evidence_status = "H005", "HYPOTHESIS"
    policy_id = "gate-s-v3"
    delta_band = (-0.16, 0.02)

    def propose(self, ctx: Context) -> list[Suggestion]:
        slc = self.expiry_in(ctx, 45, 60, 52) or self.single_expiry(ctx, 35, 65, 50)
        if not slc:
            return []
        em = self.em(ctx, slc.dte)
        out = []
        for body_mult, wing_mult in ((1.0, .55), (1.35, .75)):
            body = ctx.snap(ctx.spot - body_mult * em)
            width = max(wing_mult * em, ctx.spot * .006)
            hi, lo = ctx.snap(body + width), ctx.snap(body - width)
            legs = [self.leg(ctx, slc, "P", hi, +1),
                    self.leg(ctx, slc, "P", body, -2),
                    self.leg(ctx, slc, "P", lo, +1)]
            s = self.make(ctx, f"Wide OTM put fly {slc.dte}d body -{body_mult:.2g}σ",
                          legs, score=.8, rationale=[], gamma_test=False)
            rr = ctx.regime.get("term", {}).get("rr25_30d", 0.0)
            s.rationale = [f"Body {body:g} at -{body_mult:.2g}σ; symmetric wide wings {lo:g}/{hi:g}",
                           f"Designed for elevated IV plus rich put skew (rr25 {rr:+.1f}v)",
                           "Test at 25-50% of normal risk; take early IV-crush gains"]
            s.score = round(.6 + .08 * max(rr, 0) +
                            (.5 if ctx.regime.get("vol_state") in ("ELV", "STR") else 0)
                            - s.liquidity_pen, 3)
            out.append(s)
        return out


class TargetFly(Strategy):
    key, name = "target_fly", "Directional target fly"
    hypothesis_id, evidence_status = "H013", "HYPOTHESIS"
    policy_id = "gate-s-v3"

    def propose_for_bias(self, ctx: Context, bias: int) -> list[Suggestion]:
        slc = self.expiry_in(ctx, 20, 45, 32) or self.single_expiry(ctx)
        if not slc or bias == 0:
            return []
        em = self.em(ctx, slc.dte)
        out = []
        for mult in (.75, 1.0):
            body = ctx.snap(ctx.spot + bias * mult * em)
            width = max(.25 * em, ctx.spot * .003)
            cp = "C" if bias > 0 else "P"
            lo, hi = ctx.snap(body - width), ctx.snap(body + width)
            legs = [self.leg(ctx, slc, cp, lo, +1), self.leg(ctx, slc, cp, body, -2),
                    self.leg(ctx, slc, cp, hi, +1)]
            s = self.make(ctx, f"{'Bull' if bias > 0 else 'Bear'} target fly {slc.dte}d @ {body:g}",
                          legs, score=.4, rationale=[], gamma_test=False,
                          delta_band=(-.30, .30))
            reward = s.max_profit / max(s.net_mid, .01) if s.net_mid > 0 else 0
            s.rationale = [f"Body at {mult:g}σ {'above' if bias > 0 else 'below'} spot",
                           f"Debit-risk structure; model reward/debit {reward:.1f}x",
                           "Lotto discipline: risk at most 0.25% NLV; no repair"]
            s.score = round(min(reward, 12) * .08 - s.liquidity_pen, 3)
            out.append(s)
        return out

    def propose(self, ctx: Context) -> list[Suggestion]:
        bias = 1 if ctx.regime.get("bias", 0) > 0 else -1 if ctx.regime.get("bias", 0) < 0 else 0
        return self.propose_for_bias(ctx, bias)
