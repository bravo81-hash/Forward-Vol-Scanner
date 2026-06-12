from core.models import Context, Suggestion
from .base import Strategy


class Diagonal(Strategy):
    key, name = "diagonal", "Diagonal"

    def propose(self, ctx: Context) -> list[Suggestion]:
        if not ctx.pairs:
            return []
        up = ctx.regime["trend"] == "UP" or (ctx.regime["trend"] == "RNG" and ctx.regime["bias"] > 0)
        cp = "C" if up else "P"
        sgn = 1 if up else -1
        p = ctx.pairs[0]
        f = next(s for s in ctx.slices if s.expiry.isoformat() == p["front"])
        b = next(s for s in ctx.slices if s.expiry.isoformat() == p["back"])
        em = self.em(ctx, f.dte)
        out = []
        for lk, sk, lbl in ((0.2, 1.1, "standard"), (0.0, 1.3, "ATM long / wider short")):
            k_long = ctx.snap(ctx.spot + sgn * lk * em)
            k_short = ctx.snap(ctx.spot + sgn * sk * em)
            legs = [self.leg(ctx, b, cp, k_long, +1), self.leg(ctx, f, cp, k_short, -1)]
            why = [f"{'Call' if up else 'Put'} side with trend ({ctx.regime['trend']}, bias {ctx.regime['bias']:+d})",
                   f"Short {f.dte}d {k_short:g} sits outside the {f.dte}d EM ({em:.0f} pts)",
                   f"Pair curve edge {p['edge']:+.2f}v"]
            out.append(self.make(ctx, f"{cp} diagonal {lbl}", legs,
                                 score=p["edge"] * 0.8 + abs(ctx.regime["bias"]) * 0.3,
                                 rationale=why))
        return out
