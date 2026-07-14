"""Upside call broken-wing butterfly: entry row and standing upside repair."""
from core.models import Context, Suggestion
from .base import Strategy


class CallBWB(Strategy):
    key, name = "call_bwb", "Call BWB"
    hypothesis_id, evidence_status = "H003", "HYPOTHESIS"
    policy_id = "gate-s-v3"
    delta_band = (-0.12, 0.04)

    def propose(self, ctx: Context) -> list[Suggestion]:
        slc = self.expiry_in(ctx, 45, 60, 52) or self.single_expiry(ctx, 35, 65, 50)
        if not slc:
            return []
        em, out = self.em(ctx, slc.dte), []
        for body_mult, broken in ((.55, 1.5), (.8, 2.0)):
            lower = ctx.snap(ctx.spot + .10 * em)
            body = ctx.snap(ctx.spot + body_mult * em)
            near = max(body - lower, ctx.spot * .004)
            upper = ctx.snap(body + broken * near)
            legs = [self.leg(ctx, slc, "C", lower, +1),
                    self.leg(ctx, slc, "C", body, -2),
                    self.leg(ctx, slc, "C", upper, +1)]
            s = self.make(ctx, f"Call BWB {slc.dte}d body +{body_mult:.2g}σ",
                          legs, score=.5, rationale=[])
            rr = ctx.regime.get("term", {}).get("rr25_30d", 0.0)
            s.rationale = [f"+1 {lower:g} / -2 {body:g} / +1 {upper:g}; upper wing {broken:g}x",
                           f"Call-side candidate for rr25 {rr:+.1f}v and upside T+0 repair",
                           "Single expiry; compare entry debit and upside tail in OptionNet Explorer"]
            s.score = round(.6 + max(-rr, 0) * .2 - s.liquidity_pen, 3)
            out.append(s)
        return out
