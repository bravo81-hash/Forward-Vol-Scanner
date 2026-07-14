"""Same-expiry M3-style put BWB plus a 70-80 delta ITM call."""
from core.models import Context, Suggestion
from .base import Strategy


class M3BWBCall(Strategy):
    key, name = "m3_bwb_call", "Put BWB + ITM call"
    hypothesis_id, evidence_status = "H007", "HYPOTHESIS"
    policy_id = "gate-s-v3"
    delta_band = (-0.05, 0.12)

    def propose(self, ctx: Context) -> list[Suggestion]:
        slc = self.expiry_in(ctx, 60, 80, 70) or self.expiry_in(ctx, 45, 85, 60)
        if not slc:
            return []
        em, out = self.em(ctx, slc.dte), []
        for call_pct, ratio_label in ((.035, "1 call / fly"), (.05, "deeper call")):
            upper = ctx.snap(ctx.spot - .15 * em)
            body = ctx.snap(ctx.spot - .65 * em)
            near = max(upper - body, ctx.spot * .004)
            lower = ctx.snap(body - 2.0 * near)
            call_k = ctx.snap(ctx.spot * (1 - call_pct))
            legs = [self.leg(ctx, slc, "P", upper, +1),
                    self.leg(ctx, slc, "P", body, -2),
                    self.leg(ctx, slc, "P", lower, +1),
                    self.leg(ctx, slc, "C", call_k, +1)]
            s = self.make(ctx, f"M3-style BWB + ITM call {slc.dte}d ({ratio_label})",
                          legs, score=.7, rationale=[])
            s.rationale = [f"Put BWB {upper:g}/-{body:g}x2/{lower:g} plus long {call_k:g}C",
                           "Same expiry: ITM call supplies positive delta and flattens upside T+0",
                           "Manual OptionNet validation required: test call depth, BWB width, and roll-up behaviour"]
            s.score = round(.7 + (.35 if ctx.regime.get("bias", 0) > 0 else 0)
                            - s.liquidity_pen, 3)
            out.append(s)
        return out
