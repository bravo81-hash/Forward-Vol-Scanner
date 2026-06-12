from core.models import Context, Suggestion
from .base import Strategy


class IronCondor(Strategy):
    key, name = "condor", "Iron condor"

    def propose(self, ctx: Context) -> list[Suggestion]:
        slc = self.single_expiry(ctx)
        if not slc:
            return []
        em = self.em(ctx, slc.dte)
        wing = max(round(ctx.spot * 0.01, 0), 1.0)
        out = []
        for mult, lbl in ((1.0, "shorts at 1.0xEM"), (1.2, "shorts at 1.2xEM (safer)")):
            sp, sc = ctx.snap(ctx.spot - em * mult), ctx.snap(ctx.spot + em * mult)
            legs = [self.leg(ctx, slc, "P", sp, -1), self.leg(ctx, slc, "P", sp - wing, +1),
                    self.leg(ctx, slc, "C", sc, -1), self.leg(ctx, slc, "C", sc + wing, +1)]
            sug = self.make(ctx, f"IC {slc.dte}d {lbl}", legs, score=0.0, rationale=[])
            credit = -sug.net_mid
            cw = credit / wing if wing else 0
            sug.rationale = [
                f"Shorts {sp:g}/{sc:g} outside the {slc.dte}d expected move ({em:.0f} pts)",
                f"Credit {credit:.2f} = {cw:.0%} of {wing:g}-wide wings",
                f"VRP {ctx.regime['vrp']:+.1f}v — rent is overpriced"]
            sug.score = round(cw * 3 + max(ctx.regime["vrp"], 0) * 0.2
                              - sug.liquidity_pen, 3)
            if cw < 0.28:
                sug.rationale.append("Credit under 1/3 width — borderline, consider skip")
                sug.score -= 0.5
            out.append(sug)
        return out
