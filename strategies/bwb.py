from core.models import Context, Suggestion
from .base import Strategy


class PutBWB(Strategy):
    key, name = "bwb", "Put BWB"

    def propose(self, ctx: Context) -> list[Suggestion]:
        slc = self.single_expiry(ctx)
        if not slc:
            return []
        out = []
        for body_pct, lbl in ((0.02, "body -2%"), (0.027, "body -2.7% (deeper)")):
            up = ctx.snap(ctx.spot * 0.998)
            body = ctx.snap(ctx.spot * (1 - body_pct))
            wid = up - body
            low = ctx.snap(body - 1.5 * wid)
            legs = [self.leg(ctx, slc, "P", up, +1), self.leg(ctx, slc, "P", body, -2),
                    self.leg(ctx, slc, "P", low, +1)]
            sug = self.make(ctx, f"BWB {slc.dte}d {lbl}", legs, score=0.0, rationale=[])
            rr = slc.rr25
            sug.rationale = [
                f"+1 {up:g} / -2 {body:g} / +1 {low:g} — lower wing stretched 1.5x",
                f"Entry {('credit ' + format(-sug.net_mid, '.2f')) if sug.net_mid < 0 else ('debit ' + format(sug.net_mid, '.2f'))} — skew (rr25 {rr:.1f}v) finances the stretch",
                "No upside risk at credit; vol crush + vanna tailwind"]
            sug.score = round((1.5 if sug.net_mid <= 0 else 0.2)
                              + rr * 0.15 + max(ctx.regime["vrp"], 0) * 0.15
                              - sug.liquidity_pen, 3)
            if sug.net_mid > 0:
                sug.rationale.append("Debit entry — skew not paying today, prefer the credit variant or skip")
            out.append(sug)
        return out
