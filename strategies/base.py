"""Strategy contract + shared construction helpers."""
from __future__ import annotations
import math
from abc import ABC, abstractmethod
from datetime import date

from core.chain import iv_at
from core.models import Context, Leg, Slice, Suggestion
from core.pricing import struct_greeks, struct_metrics

HOLD_MAX = 10
FRONT_EXIT_DTE = 7      # any short leg must clear this at latest exit


class Strategy(ABC):
    key = "base"
    name = "Base"

    @abstractmethod
    def propose(self, ctx: Context) -> list[Suggestion]:
        ...

    # ---------------------------------------------------------- helpers ----
    @staticmethod
    def leg(ctx: Context, slc: Slice, cp: str, strike: float, qty: int) -> Leg:
        k = ctx.snap(strike)
        return Leg(cp=cp, strike=k, expiry=slc.expiry, qty=qty, iv=iv_at(slc, k))

    def make(self, ctx: Context, label: str, legs: list[Leg],
             score: float, rationale: list[str],
             delta_band: tuple[float, float] | None = None,
             gamma_test: bool = True) -> Suggestion:
        m = struct_metrics(ctx.spot, legs, ctx.today, q=ctx.q)
        g = struct_greeks(ctx.spot, legs, ctx.today, q=ctx.q)
        liq = self.liquidity_pen(ctx, legs)
        s = Suggestion(strategy=self.key, label=label, legs=legs,
                       net_mid=m["entry"], greeks=g,
                       max_profit=m["max_profit"], max_loss=m["max_loss"],
                       breakevens=m["breakevens"],
                       score=round(score - liq, 3), rationale=rationale,
                       liquidity_pen=round(liq, 3))
        self._check_targets(ctx, s, delta_band or self.delta_band, gamma_test)
        return s

    delta_band: tuple[float, float] | None = None

    def _check_targets(self, ctx: Context, s: Suggestion,
                       band: tuple[float, float] | None, gamma_test: bool):
        """Entry-Greeks targets (mirrors the playbook guide tables).
        Off-target -> rationale flag + score penalty, never a hard block."""
        flags = []
        if band:
            lo, hi = band
            d = s.greeks["delta"]
            if not lo <= d <= hi:
                lever = ("shift strike toward the band" if self.key in
                         ("calendar", "double_calendar", "diagonal")
                         else "recenter shorts / rebalance by delta")
                flags.append(f"Δ {d:+.2f} outside target {lo:+.2f}..{hi:+.2f} — {lever}")
        if gamma_test and s.greeks["theta"] > 0:
            em1 = ctx.spot * ctx.regime["rv21"] / 100 / math.sqrt(252)
            gloss = 0.5 * abs(s.greeks["gamma"]) * em1 * em1
            if gloss > s.greeks["theta"]:
                flags.append(f"Gamma breakeven FAILS: avg-day gamma loss "
                             f"{gloss:.2f} > theta {s.greeks['theta']:.2f} — "
                             "push expiry out or widen")
        for f in flags:
            s.rationale.append("TARGET: " + f)
        s.score = round(s.score - 0.4 * len(flags), 3)

    @staticmethod
    def liquidity_pen(ctx: Context, legs: list[Leg]) -> float:
        """Penalty in score units from ATM NBBO spread% x leg count."""
        spr = 0.0
        for l in legs:
            slc = next((s for s in ctx.slices if s.expiry == l.expiry), None)
            spr += (slc.atm_spread_pct if slc else 0.10)
        return spr * 1.5

    @staticmethod
    def em(ctx: Context, days: int) -> float:
        """Expected move (1 sigma) over `days` from 21d realized vol."""
        return ctx.spot * ctx.regime["rv21"] / 100 * math.sqrt(days / 252)

    @staticmethod
    def single_expiry(ctx: Context, lo=21, hi=38, target=30) -> Slice | None:
        from core.events import fomc_within
        cands = [s for s in ctx.slices
                 if lo <= s.dte <= hi and s.dte - HOLD_MAX >= FRONT_EXIT_DTE]
        clean = [s for s in cands if not (fomc_within(s.expiry, ctx.today) and s.dte <= 21)]
        pool = clean or cands
        return min(pool, key=lambda s: abs(s.dte - target)) if pool else None
