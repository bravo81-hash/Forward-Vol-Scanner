"""Same-expiry directional debit spreads for defensive states and repairs."""
from __future__ import annotations

from core.models import Context, Suggestion
from .base import Strategy


class DirectionalDebitSpread(Strategy):
    key, name = "debit_spread", "Directional debit spread"
    hypothesis_id, evidence_status = "H014", "HYPOTHESIS"
    policy_id = "campaign-ranker-v4"

    def propose_for_bias(self, ctx: Context, bias: int) -> list[Suggestion]:
        slc = self.expiry_in(ctx, 30, 60, 45) or self.single_expiry(ctx)
        if not slc or bias == 0:
            return []
        em, out = self.em(ctx, slc.dte), []
        cp = "C" if bias > 0 else "P"
        for short_mult, label in ((0.65, "standard"), (0.90, "wide target")):
            long_k = ctx.snap(ctx.spot - bias * 0.10 * em)
            short_k = ctx.snap(ctx.spot + bias * short_mult * em)
            legs = [self.leg(ctx, slc, cp, long_k, +1),
                    self.leg(ctx, slc, cp, short_k, -1)]
            band = (0.10, 0.45) if bias > 0 else (-0.45, -0.10)
            s = self.make(ctx,
                          f"{'Bull call' if bias > 0 else 'Bear put'} debit spread "
                          f"{slc.dte}d ({label})",
                          legs, score=.6, rationale=[], delta_band=band,
                          gamma_test=False)
            reward = s.max_profit / max(s.net_mid, .01) if s.net_mid > 0 else 0.0
            s.rationale = [
                f"Same expiry {long_k:g}/{short_k:g}{cp}; defined debit and no short tail",
                f"Directional participation for unpaid-carry/defensive states; reward/debit {reward:.1f}x",
                "Also test as a debit-only adjustment; compare with closing rather than automatically repairing",
            ]
            s.score = round(min(reward, 6) * .15 - s.liquidity_pen, 3)
            out.append(s)
        return out

    def propose(self, ctx: Context) -> list[Suggestion]:
        bias = 1 if ctx.regime.get("bias", 0) > 0 else -1 if ctx.regime.get("bias", 0) < 0 else 0
        return self.propose_for_bias(ctx, bias)
