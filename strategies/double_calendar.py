from core.models import Context, Suggestion
from .base import Strategy


class DoubleCalendar(Strategy):
    key, name = "double_calendar", "Double calendar"
    delta_band = (-0.05, 0.05)

    def propose(self, ctx: Context) -> list[Suggestion]:
        if not ctx.pairs:
            return []
        p = ctx.pairs[0]
        f = next(s for s in ctx.slices if s.expiry.isoformat() == p["front"])
        b = next(s for s in ctx.slices if s.expiry.isoformat() == p["back"])
        out = []
        for mult, lbl in ((1.0, "EM-edge strikes"), (0.75, "tighter 0.75xEM strikes")):
            w = self.em(ctx, f.dte) * mult
            kp, kc = ctx.snap(ctx.spot - w), ctx.snap(ctx.spot + w)
            legs = [self.leg(ctx, f, "P", kp, -1), self.leg(ctx, b, "P", kp, +1),
                    self.leg(ctx, f, "C", kc, -1), self.leg(ctx, b, "C", kc, +1)]
            why = [f"Tent strikes {kp:g}/{kc:g} straddle the {f.dte}d expected move",
                   f"Curve edge {p['edge']:+.2f}v on pair {p['f_dte']}/{p['b_dte']}d"]
            if p.get("fomc_between"):
                why.append("FOMC sits BETWEEN the legs — long back vega through the print")
            out.append(self.make(ctx, f"Dbl cal {lbl}", legs,
                                 score=p["edge"] * 0.9, rationale=why))
        return out
