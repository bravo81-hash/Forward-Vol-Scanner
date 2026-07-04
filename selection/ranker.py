"""Regime matrix -> top-2 strategy families -> 2 candidates each.

Mirrors TE Console v2.2 priority order exactly (one source of truth):
gates -> family priority list -> per-family candidates -> composite score.

Doctrine is ADVISORY, not a kill-switch: the rules decide which families
rank best and raise gate warnings, but they never suppress output. Even a
stressed surface, negative VRP, or a Friday session still returns the
best-available candidates — the warnings (gates + verdict) tell the user
to stand aside; they don't make that decision for them.
"""
from __future__ import annotations

from core.models import Context, Suggestion
from core.surface import event_premium
from selection.manage import management_plan
from strategies import REGISTRY

MULTI_EXPIRY = {"calendar", "double_calendar", "diagonal"}   # F1: SMSF-blockable


def verdict_size(verdict: str, regime: dict, gates: list[dict]) -> str:
    """F4: collapse the verdict/regime into a machine size field."""
    v = verdict or ""
    if v.startswith("WARNING") or "STAND" in v.upper():
        return "STAND"
    if "quarter" in v.lower():
        return "QUARTER"
    if ("half" in v.lower() or regime.get("gamma") == "-g"
            or regime.get("vol_state") == "ELV"
            or any(g["code"] == "F" for g in gates)):
        return "HALF"
    return "FULL"

VRP_HARVEST_FLOOR = -1.5    # event harvest allowed down to this VRP only
FRIDAY_OK = {"calendar", "double_calendar", "diagonal"}   # net-debit long-vega


def family_priority(ctx: Context) -> tuple[list[tuple[str, str]], str]:
    """Returns ([(family_key, reason)...] best-first, verdict).

    Never returns an empty list on account of a gate — suggestions are
    always shown and the doctrine speaks through warnings instead.
    """
    r, ev = ctx.regime, ctx.events
    hard = [g for g in ctx.gates if g["hard"]]

    fams: list[tuple[str, str]] = []
    neg_g = r["gamma"] == "-g"
    vrp_ok = r["vrp"] > 0
    term = r.get("term", {}).get("verdict", "FLAT")
    verdict = None

    if neg_g:
        fams.append(("bwb", "-g tape: defined-risk skew-financed only, HALF SIZE"))
    if not vrp_ok:
        # FOMC event-harvest exception: backward-looking RV must not veto
        # selling a demonstrably rich event kink. Guards: VRP not deeply
        # negative, front inverted, FOMC close, implied move rich vs history.
        if (r["vrp"] >= VRP_HARVEST_FLOOR and term == "INVERTED FRONT"
                and ev["fomc_dte"] <= 21):
            evp = event_premium(ctx.slices, ctx.today, r["rv21"])
            if evp and evp["rich"]:
                ctx.events["harvest"] = evp     # shortlist routes on this
                fams.append(("calendar",
                             f"FOMC harvest: implied event move "
                             f"{evp['implied_move_pct']:.2f}% >= 1.25x hist "
                             f"0.9% — sell the kinked front"))
                verdict = "TRADE — CAUTION (event harvest, half size)"
        if verdict is None:
            if term in ("STEEP CONTANGO", "CONTANGO"):
                fams.append(("calendar", "VRP negative — debit time spread preferred"))
            verdict = "CAUTION — VRP negative, premium selling unpaid"
    else:
        if ev["opex_week"] and r["trend"] == "RNG" and r["gamma"] == "+g":
            fams.append(("butterfly", "OpEx pin window (+g, range)"))
        if r["vol_state"] == "STR" and r["vrp"] > 2 and r["rv_falling"]:
            # T2: graduate STR. Stressed-but-RICH (VRP > +2, realized falling)
            # are the best-paid selling days; hard gate still sets the WARNING
            # verdict, but the card is a defined-risk BWB at QUARTER size
            # instead of a generic fallback.
            fams.append(("bwb", f"Stressed-but-rich: VRP {r['vrp']:+.1f}v with RV "
                                "falling — defined-risk BWB, QUARTER size"))
        if r["vol_state"] == "ELV":
            fams.append(("bwb", "Elevated IV — skew finances wings, vol crush pays"))
            fams.append(("condor", "Elevated IV alternative if rangebound"))
        if r["vol_state"] == "NRM" and r["trend"] == "RNG":
            fams.append(("condor", f"Normal vol, range, VRP {r['vrp']:+.1f}v"))
            fams.append(("butterfly", "OTM fly if Direction leans down"))
        if r["vol_state"] == "NRM" and r["trend"] in ("UP", "DN"):
            # T1: the missing matrix cell — 12.7% of backtest days fell here
            # and every one had VRP > 0. Trend-side theta, same shape as the
            # CMP+trend cell.
            fams.append(("diagonal", f"Normal vol + {r['trend']} trend — trend-side theta"))
            fams.append(("calendar", "No-direction fallback"))
        if r["vol_state"] == "CMP" and r["trend"] == "RNG":
            fams.append(("calendar", "Calm + range: cheap back vega, theta differential"))
            fams.append(("double_calendar", "Wider tent variant"))
        if r["vol_state"] == "CMP" and r["trend"] in ("UP", "DN"):
            fams.append(("diagonal", f"Calm + {r['trend']} trend"))
            fams.append(("calendar", "No-direction fallback"))
        if term == "INVERTED FRONT":
            fams.append(("calendar", "Front inverted — sell the rich front leg"))
        verdict = ("TRADE — CAUTION (half size)" if neg_g or r["vol_state"] == "ELV"
                   or any(g["code"] == "F" for g in ctx.gates) else "TRADE")

    seen, ordered = set(), []
    for k, why in fams:
        if k not in seen:
            ordered.append((k, why))
            seen.add(k)
    if not ordered:                      # no doctrine edge — show best-available
        if verdict is None:              # don't downgrade an existing CAUTION
            verdict = "MARGINAL — no clear edge, skipping is fine"
        ordered = [("condor", "No clear edge; condor only if credit/width clears 1/3"),
                   ("calendar", "Best available pair, thin edge")]
        if r["vrp"] <= 0:                # selling is unpaid — lead with the debit spread
            ordered.reverse()
    if not ctx.pairs and all(k in ("calendar", "double_calendar", "diagonal")
                             for k, _ in ordered):
        # T4: every ranked family needs a calendar pair and none exists today —
        # guarantee one single-expiry defined-risk card instead of an empty board.
        ordered.insert(1, ("butterfly",
                           "Fallback: no valid calendar pair today — single-expiry defined-risk"))
    if any(g["code"] == "W" for g in ctx.gates):    # Friday: prefer net-debit
        # doctrine prefers long-vega debit on Fridays — rank those first as a
        # nudge, but DO NOT remove the others; the W gate carries the warning.
        ordered.sort(key=lambda kw: kw[0] not in FRIDAY_OK)
    if hard:                             # surface unsettled / stressed / unstable
        # hard gates are the strongest warning, not a block: cards still show.
        verdict = ("WARNING — " + "; ".join(g["msg"] for g in hard)
                   + " — suggestions shown; confirm and size down")
    return ordered[:2], verdict


def shortlist(ctx: Context) -> dict:
    from portfolio.risk import lots_for

    fams, verdict = family_priority(ctx)
    size = verdict_size(verdict, ctx.regime, ctx.gates)

    # F1: mandate — drop multi-expiry families this account cannot trade on an
    # EU cash-settled index, and record why (never silently swallow).
    mandate = ctx.mandate or {}
    blocked_note = None
    if mandate.get("block_multi_expiry"):
        kept = [(k, w) for k, w in fams if k not in MULTI_EXPIRY]
        if len(kept) != len(fams):
            dropped = sorted({k for k, _ in fams if k in MULTI_EXPIRY})
            blocked_note = (f"{mandate.get('account', 'account')}: "
                            f"{', '.join(dropped)} blocked — multi-expiry combo on "
                            f"EU cash-settled index; single-expiry only")
            fams = kept
        if not fams:                              # nothing left → single-expiry fallback
            fams = [("condor", "Single-expiry only (mandate): defined-risk condor"),
                    ("butterfly", "Single-expiry defined-risk fly")]

    cards: list[Suggestion] = []
    for key, why in fams:
        strat = REGISTRY[key]
        if why.startswith("FOMC harvest") and ctx.events.get("harvest"):
            props = strat.propose_event(ctx, ctx.events["harvest"])
        else:
            props = strat.propose(ctx)
        cands = sorted(props, key=lambda s: s.score, reverse=True)[:2]
        for c in cands:
            c.rationale.insert(0, why)
            if ctx.book:
                c.fit = _fit(ctx, c)
                c.score = round(c.score + c.fit, 3)
            card_size = "QUARTER" if (key == "bwb" and "QUARTER" in why) else size
            c.manage = management_plan(ctx, c)                    # P2
            nlv = ctx.book.get("nlv") if isinstance(ctx.book, dict) else None
            c.lots = lots_for(c.greeks, nlv, card_size, ctx.book)  # P1
            c.lots["size"] = card_size
        cards.extend(cands)
    cards.sort(key=lambda s: s.score, reverse=True)

    if blocked_note:
        verdict = f"{verdict}  ·  {blocked_note}"
    return {"symbol": ctx.symbol, "verdict": verdict, "size": size,
            "regime": ctx.regime, "events": ctx.events,
            "gates": ctx.gates, "pairs": ctx.pairs[:4],
            "mandate": mandate, "data": ctx.data,
            "cards": [c.to_dict() for c in cards]}


def _fit(ctx: Context, s: Suggestion) -> float:
    """Portfolio-fit: reward suggestions that pull book vega toward 0 band."""
    from portfolio.risk import fit_score
    return fit_score(ctx.book, s)
