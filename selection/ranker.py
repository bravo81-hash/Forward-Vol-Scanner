"""Regime matrix -> top-2 strategy families -> 2 candidates each.

Mirrors TE Console v2.2 priority order exactly (one source of truth):
gates -> family priority list -> per-family candidates -> composite score.
"""
from __future__ import annotations

from core.models import Context, Suggestion
from strategies import REGISTRY


def family_priority(ctx: Context) -> tuple[list[tuple[str, str]], str]:
    """Returns ([(family_key, reason)...] best-first, verdict)."""
    r, ev = ctx.regime, ctx.events
    hard = [g for g in ctx.gates if g["hard"]]
    if hard:
        return [], "STAND ASIDE — " + "; ".join(g["msg"] for g in hard)

    fams: list[tuple[str, str]] = []
    neg_g = r["gamma"] == "-g"
    vrp_ok = r["vrp"] > 0
    term = r.get("term", {}).get("verdict", "FLAT")

    if neg_g:
        fams.append(("bwb", "-g tape: defined-risk skew-financed only, HALF SIZE"))
    if not vrp_ok:
        if term in ("STEEP CONTANGO", "CONTANGO"):
            fams.append(("calendar", "VRP negative — debit time spread is the only allowed family"))
        verdict = "CAUTION — VRP negative, premium selling unpaid"
        return fams[:2], verdict if fams else "STAND ASIDE — VRP negative, no debit edge either"

    if ev["opex_week"] and r["trend"] == "RNG" and r["gamma"] == "+g":
        fams.append(("butterfly", "OpEx pin window (+g, range)"))
    if r["vol_state"] == "ELV":
        fams.append(("bwb", "Elevated IV — skew finances wings, vol crush pays"))
        fams.append(("condor", "Elevated IV alternative if rangebound"))
    if r["vol_state"] == "NRM" and r["trend"] == "RNG":
        fams.append(("condor", f"Normal vol, range, VRP {r['vrp']:+.1f}v"))
        fams.append(("butterfly", "OTM fly if Direction leans down"))
    if r["vol_state"] == "CMP" and r["trend"] == "RNG":
        fams.append(("calendar", "Calm + range: cheap back vega, theta differential"))
        fams.append(("double_calendar", "Wider tent variant"))
    if r["vol_state"] == "CMP" and r["trend"] in ("UP", "DN"):
        fams.append(("diagonal", f"Calm + {r['trend']} trend"))
        fams.append(("calendar", "No-direction fallback"))
    if term == "INVERTED FRONT":
        fams.append(("calendar", "Front inverted — sell the rich front leg"))

    seen, ordered = set(), []
    for k, why in fams:
        if k not in seen:
            ordered.append((k, why))
            seen.add(k)
    verdict = ("TRADE — CAUTION (half size)" if neg_g or r["vol_state"] == "ELV"
               or any(g["code"] == "F" for g in ctx.gates) else "TRADE")
    if not ordered:
        verdict = "MARGINAL — skipping is fine"
        ordered = [("condor", "No clear edge; condor only if credit/width clears 1/3"),
                   ("calendar", "Best available pair, thin edge")]
    return ordered[:2], verdict


def shortlist(ctx: Context) -> dict:
    fams, verdict = family_priority(ctx)
    cards: list[Suggestion] = []
    for key, why in fams:
        strat = REGISTRY[key]
        cands = sorted(strat.propose(ctx), key=lambda s: s.score, reverse=True)[:2]
        for c in cands:
            c.rationale.insert(0, why)
            if ctx.book:
                c.fit = _fit(ctx, c)
                c.score = round(c.score + c.fit, 3)
        cards.extend(cands)
    cards.sort(key=lambda s: s.score, reverse=True)
    return {"symbol": ctx.symbol, "verdict": verdict,
            "regime": ctx.regime, "events": ctx.events,
            "gates": ctx.gates, "pairs": ctx.pairs[:4],
            "cards": [c.to_dict() for c in cards]}


def _fit(ctx: Context, s: Suggestion) -> float:
    """Portfolio-fit: reward suggestions that pull book vega toward 0 band."""
    from portfolio.risk import fit_score
    return fit_score(ctx.book, s)
