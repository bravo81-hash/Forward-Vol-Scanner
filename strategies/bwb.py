from core.models import Context, Suggestion
from .base import Strategy


class PutBWB(Strategy):
    key, name = "bwb", "Put BWB"
    hypothesis_id, evidence_status = "H001", "HYPOTHESIS"
    policy_id = "gate-s-v3"
    delta_band = (0.02, 0.10)

    def propose(self, ctx: Context) -> list[Suggestion]:
        slc = self.expiry_in(ctx, 60, 80, 70) or self.expiry_in(ctx, 45, 85, 60)
        if not slc:
            return []
        em, out = self.em(ctx, slc.dte), []
        for body_mult, broken, lbl in ((.55, 2.0, "standard"), (.80, 2.5, "deeper")):
            up = ctx.snap(ctx.spot - .10 * em)
            body = ctx.snap(ctx.spot - body_mult * em)
            wid = max(up - body, ctx.spot * .004)
            low = ctx.snap(body - broken * wid)
            legs = [self.leg(ctx, slc, "P", up, +1), self.leg(ctx, slc, "P", body, -2),
                    self.leg(ctx, slc, "P", low, +1)]
            sug = self.make(ctx, f"Put BWB {slc.dte}d {lbl}", legs, score=0.0, rationale=[])
            rr = slc.rr25
            sug.rationale = [
                f"+1 {up:g} / -2 {body:g} / +1 {low:g} — lower wing stretched {broken:g}x",
                f"Entry {('credit ' + format(-sug.net_mid, '.2f')) if sug.net_mid < 0 else ('debit ' + format(sug.net_mid, '.2f'))} — skew (rr25 {rr:.1f}v) finances the stretch",
                "60–80 DTE campaign row; exit/review by 30–35 DTE and test debit-only repairs"]
            sug.score = round((1.5 if sug.net_mid <= 0 else 0.2)
                              + rr * 0.15 + max(ctx.regime["vrp"], 0) * 0.15
                              - sug.liquidity_pen, 3)
            if sug.net_mid > 0:
                sug.rationale.append("Debit entry — skew not paying today, prefer the credit variant or skip")
            out.append(sug)
        return out
