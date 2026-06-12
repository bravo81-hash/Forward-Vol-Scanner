from datetime import datetime
from core.events import opex_day
from core.models import Context, Suggestion
from .base import Strategy


class Butterfly(Strategy):
    """OpEx pin fly when in OpEx week (+g, range), else OTM put fly."""
    key, name = "butterfly", "Butterfly"

    def propose(self, ctx: Context) -> list[Suggestion]:
        out = []
        if ctx.events["opex_week"] and ctx.regime["gamma"] == "+g":
            ox = datetime.strptime(ctx.events["opex_date"], "%Y-%m-%d").date()
            slc = next((s for s in ctx.slices if s.expiry == ox), None) or \
                  min(ctx.slices, key=lambda s: abs((s.expiry - ox).days))
            w = max(round(ctx.spot * 0.008, 0), 1.0)
            k = ctx.snap(ctx.spot)
            legs = [self.leg(ctx, slc, "C", k - w, +1), self.leg(ctx, slc, "C", k, -2),
                    self.leg(ctx, slc, "C", k + w, +1)]
            sug = self.make(ctx, f"Pin fly @ {k:g} OpEx", legs, 0.0, [],
                            delta_band=(-0.05, 0.05), gamma_test=False)
            sug.rationale = [f"OpEx pin candidate at {k:g}, wings {w:g}",
                             f"Debit {sug.net_mid:.2f} vs max {sug.max_profit:.2f} — "
                             f"{(sug.max_profit / max(sug.net_mid, .01)):.1f}x if pinned",
                             "+g tape: dealers fade moves into OpEx"]
            sug.score = round(min(sug.max_profit / max(sug.net_mid, 0.01), 8) * 0.3
                              - sug.liquidity_pen, 3)
            out.append(sug)
        slc = self.single_expiry(ctx)
        if slc:
            em = self.em(ctx, slc.dte)
            body = ctx.snap(ctx.spot - em)
            w = max(round(em * 0.8, 0), 1.0)
            legs = [self.leg(ctx, slc, "P", ctx.snap(body + w), +1),
                    self.leg(ctx, slc, "P", body, -2),
                    self.leg(ctx, slc, "P", ctx.snap(body - w), +1)]
            sug = self.make(ctx, f"OTM put fly {slc.dte}d body {body:g}", legs, 0.0, [],
                            delta_band=(-0.08, -0.03), gamma_test=False)
            sug.rationale = [f"Body at lower EM edge — where a normal down-move lands",
                             f"Debit {sug.net_mid:.2f} for max {sug.max_profit:.2f}",
                             f"Bias {ctx.regime['bias']:+d} — drift-down convexity"]
            sug.score = round(min(sug.max_profit / max(sug.net_mid, 0.01), 10) * 0.25
                              + (0.5 if ctx.regime["bias"] < 0 else 0.0)
                              - sug.liquidity_pen, 3)
            out.append(sug)
        return out[:2]
