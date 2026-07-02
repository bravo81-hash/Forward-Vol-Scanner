from core.models import Context, Suggestion
from core.surface import FOMC_HIST_MOVE_PCT, HARVEST_MIN_RATIO
from .base import Strategy


class Calendar(Strategy):
    key, name = "calendar", "Calendar"
    delta_band = (-0.10, 0.10)

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
            if p.get("fomc_between"):
                why.append("FOMC sits BETWEEN the legs — long back vega through the print")
            if p["edge"] < 0.5:
                why.append("THIN edge — chain confirmation mandatory")
            out.append(self.make(ctx, f"ATM put cal {p['f_dte']}/{p['b_dte']}d",
                                 legs, score=p["edge"], rationale=why))
        return out

    def propose_event(self, ctx: Context, ev: dict) -> list[Suggestion]:
        """FOMC harvest calendars — bypass ctx.pairs and the FRONT_DTE band.

        Front = first expiry ON/AFTER the FOMC date (it carries the event
        premium); back = next expiries 7-21 days beyond it. Short-duration
        trade: the normal hold and front-exit rules are overridden in the
        rationale, so gamma_test is off.
        """
        f = ev["f2"]
        backs = [s for s in ctx.slices
                 if 7 <= (s.expiry - f.expiry).days <= 21][:2]
        k = ctx.snap(ctx.spot)
        out = []
        for b in backs:
            legs = [self.leg(ctx, f, "P", k, -1), self.leg(ctx, b, "P", k, +1)]
            why = [f"Implied FOMC move {ev['implied_move_pct']:.2f}% vs "
                   f"{FOMC_HIST_MOVE_PCT:.1f}% historical "
                   f"(needs >= {HARVEST_MIN_RATIO:.2f}x) — front is kinked rich",
                   "Short-duration event trade: exit within 1-2 sessions after "
                   "FOMC. Normal 5-10 day hold and 7-DTE front-exit rules "
                   "DO NOT apply."]
            out.append(self.make(
                ctx, f"EVENT CAL {f.dte}d/{b.dte}d — exit day after FOMC",
                legs,
                score=ev["implied_move_pct"] - HARVEST_MIN_RATIO * FOMC_HIST_MOVE_PCT,
                rationale=why, gamma_test=False))
        return out
