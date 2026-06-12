from core.models import Context, Suggestion
from .base import Strategy


class Calendar(Strategy):
    key, name = "calendar", "Calendar"

    def propose(self, ctx: Context) -> list[Suggestion]:
        out = []
        for p in ctx.pairs[:2]:
            f = next(s for s in ctx.slices if s.expiry.isoformat() == p["front"])
            b = next(s for s in ctx.slices if s.expiry.isoformat() == p["back"])
            k = ctx.snap(ctx.spot)
            legs = [self.leg(ctx, f, "P", k, -1), self.leg(ctx, b, "P", k, +1)]
            why = [f"Curve edge {p['edge']:+.2f} vol pts (front {p['f_iv']} vs fwd {p['fwd']})",
                   f"Sell {p['front']} ({p['f_dte']}d) / buy {p['back']} ({p['b_dte']}d)"]
            if p["fomc_in_front"]:
                why.append("FOMC inside front leg — selling event premium, confirm front is rich")
            if p["edge"] < 0.5:
                why.append("THIN edge — chain confirmation mandatory")
            out.append(self.make(ctx, f"ATM put cal {p['f_dte']}/{p['b_dte']}d",
                                 legs, score=p["edge"], rationale=why))
        return out
