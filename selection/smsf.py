"""SMSF module — Gate S: single-expiry structure selection for a cash account.

Doctrine
--------
The SMSF book is an IBKR *cash* account: EU cash-settled index (SPX/RUT/XSP)
multi-expiry combos are blocked, so calendars/diagonals/double-calendars are
off the table both at initiation AND as adjustments. Every recommendation
here is single-expiry, defined-risk, and every suggested adjustment is
debit-side (long option / debit spread / extra long wing) so it only consumes
its debit in cash.

Selection is a pure function over an already-built Context (no fetching):

    inputs:  IV band (iv_pctl bands, proxy fallback)   -- engine of the trade
             25d skew (rr25_30d, put-over-call vol pts) -- which side / broken?
             term verdict                               -- regime overlay
             bias (regime bias or user override)        -- body placement
             vrp_fwd / vrp_flip                         -- carry confirmation

    output:  ONE ACTION line + ranked single-expiry variants, each carrying
             its full build spec (DTE, body, wings, cash note, management,
             single-expiry adjustment doctrine).

Same doctrine as direction.py: ADVISORY, never a block. Hard gates from the
Context are surfaced as notes, not suppression.
"""
from __future__ import annotations

from core.models import Context
from selection.direction import vol_band

# rr25 below this (put IV *under* call IV) = call side is the bid
CALL_SKEW_BID = -0.5    # vol pts

# ------------------------------------------------------------- build specs --
# Single source of truth for the reference guide (UI + static playbook).
BUILD_SPECS: dict[str, dict] = {
    "put_bwb": {
        "label": "Put BWB",
        "role": "Default SMSF income engine — skew pays for the asymmetry",
        "dte": "60–80 DTE entry; exit/roll by 30–35 DTE",
        "body": "Shorts 20–40 pts below spot (≈ −0.5σ to −1σ, ~30–35Δ)",
        "wings": "Upper wing 20–30 pts above body; lower wing broken 2–3× "
                 "wider below. Steeper skew ⇒ widen the broken wing for the "
                 "same debit (or enter at a small credit)",
        "cash": "Cash held = lower-wing width × 100 − net credit (or + debit). "
                "Broken wing is what makes the cash requirement worth it",
        "manage": "Profit target 8–12% of max risk. Adjust ONLY with "
                  "debit-side adds: call debit spread above the tent on a "
                  "rally; roll/stack a second BWB lower on a break",
        "when": "Mid IV + steep put skew + neutral-to-slightly-bearish",
    },
    "m3_bwb_call": {
        "label": "Put BWB + ITM call (M3-style)",
        "role": "The single-expiry cousin of your fly + call calendar combo",
        "dte": "60–80 DTE, both components SAME expiry; exit by 30–35 DTE",
        "body": "BWB as per Put BWB; add 1 ITM call (70–80Δ) per 1–2 flies, "
                "strike ≈ 3–5% below spot",
        "wings": "BWB wings as standard; the ITM call replaces the call "
                 "calendar's job — upside T+0 flattening + positive delta",
        "cash": "Call debit adds to cash use; offsets by allowing a wider / "
                "more bearish BWB body. Net position delta ≈ flat to +5 per lot",
        "manage": "On rally: BWB loses, call pays — harvest by rolling the "
                  "call up (debit-neutral vertical roll). On selloff: call is "
                  "the sacrifice, BWB tent catches",
        "when": "Mid IV + steep skew + mildly bullish bias",
    },
    "balanced_fly": {
        "label": "Balanced ATM put fly",
        "role": "Symmetric carry when skew gives no subsidy",
        "dte": "45–60 DTE; exit by 21 DTE or at target",
        "body": "Shorts at the money (nearest listed strike to spot)",
        "wings": "Symmetric 30–50 pts each side (SPX); ~1σ of remaining DTE",
        "cash": "Cash held = one wing width × 100 − credit. Prefer the "
                "all-put version over iron fly in a cash account: same graph, "
                "half the spreads consuming cash",
        "manage": "Profit target 10–15% of max risk; no adjustment doctrine — "
                  "exit and re-centre rather than repair",
        "when": "Mid IV + flat skew + neutral",
    },
    "iron_fly": {
        "label": "Iron fly",
        "role": "Rich-premium harvest in high IV when skew is flat",
        "dte": "30–45 DTE (shorter than the BWB book); exit fast",
        "body": "Shorts straddle ATM",
        "wings": "Wide — 1.25–1.5σ; high IV means wide wings still pay",
        "cash": "CAUTION: executed as two credit spreads, a cash account "
                "holds cash on BOTH widths. The all-put balanced fly is the "
                "same risk graph at half the cash — prefer it unless call-"
                "side fills are materially better",
        "manage": "Take 25% of credit quickly; high IV entries are IV-crush "
                  "trades, not hold-to-expiry trades",
        "when": "High IV + flat skew + pinned market",
    },
    "otm_put_fly": {
        "label": "OTM put fly (wide)",
        "role": "High-IV income with a bearish tilt — profits from drift "
                "into the tent",
        "dte": "45–60 DTE; exit by 21 DTE",
        "body": "Shorts at −1σ to −1.5σ below spot",
        "wings": "Wider than normal (high IV ⇒ vol-of-vol high): 40–60 pts "
                 "SPX equivalent; size DOWN 25–50% vs mid-IV book",
        "cash": "Symmetric version for high IV (broken wing adds tail cash "
                "use exactly when tails are live)",
        "manage": "Profit target 8–10%, taken EARLY — high-IV entries mean "
                  "IV crush does the work if price sits; don't overstay",
        "when": "High IV + steep skew; also mid IV + bearish bias",
    },
    "call_bwb": {
        "label": "Call BWB / call fly (above market)",
        "role": "Sell the upside when the call wing is the expensive side",
        "dte": "45–60 DTE",
        "body": "Shorts 1–3% above spot at the richest call strikes",
        "wings": "Lower wing near spot; upper wing broken wider above",
        "cash": "Same broken-wing cash math as the put BWB, mirrored",
        "when": "Call skew bid (rr25 negative) — post-crash rebounds, "
                "squeeze conditions; more common in RUT than SPX",
        "manage": "This is the standing upside ADJUSTMENT too: where the "
                  "margin book adds a call diagonal, the SMSF book adds a "
                  "small call BWB — similar T+0 reshaping, one expiry",
    },
    "target_fly": {
        "label": "Directional target fly (long fly at a level)",
        "role": "Cheap convexity to a specific price target — a trade, "
                "not income",
        "dte": "Match DTE to the thesis horizon; 20–45 typical",
        "body": "Centred AT the target (call fly above for bullish, put fly "
                "below for bearish)",
        "wings": "Narrow (10–25 pts SPX) — maximise reward-to-risk; treat "
                 "the debit as the full expected loss",
        "cash": "Debit only — the cheapest structure in the book to hold",
        "manage": "Lotto discipline: size ≤ 0.25% NLV, no adjustments, "
                  "take 100–200% of debit or let it die",
        "when": "Low IV with a directional view, or any strong view with "
                "a level",
    },
    "debit_spread": {
        "label": "Directional debit spread",
        "role": "Defensive-state participation and debit-only adjustment",
        "dte": "30–60 DTE; match expiry to the thesis horizon",
        "body": "Long near-ATM 50–60Δ option; short near the directional target",
        "wings": "One same-expiry vertical; no uncovered tail",
        "cash": "Debit only; risk no more than 0.5% NLV",
        "manage": "Take 50–75% of debit; stop near 50%; compare every repair with closing",
        "when": "Negative forward VRP or inverted-front defensive state with a declared direction",
    },
}

VARIANT_KEYS = list(BUILD_SPECS)


# ------------------------------------------------------------------ gate S --
def _rank(order: list[tuple[str, str]]) -> list[dict]:
    return [{"rank": i + 1, "key": k, "label": BUILD_SPECS[k]["label"],
             "why": w, "build": BUILD_SPECS[k]}
            for i, (k, w) in enumerate(order)]


def gate_s(ctx: Context, bias: int) -> tuple[list[tuple[str, str]], list[str]]:
    """Ranked (key, why) variant order + notes for the given bias (-1/0/+1)."""
    reg = ctx.regime
    term = reg.get("term", {})
    band, band_src = vol_band(reg)
    rr25 = term.get("rr25_30d", 0.0)
    skew_rich = bool(term.get("skew_rich"))
    call_bid = rr25 <= CALL_SKEW_BID
    tverd = term.get("verdict", "FLAT")
    notes: list[str] = []

    hi, lo = band in ("ELV", "STR"), band == "CMP"

    # -- regime overlay first: backwardation is a de-gross condition ---------
    if tverd == "INVERTED FRONT":
        notes.append("TERM INVERTED — stress regime: all short-gamma books "
                     "correlate here; this position counts in the drawdown "
                     "ladder. Audit rule: zero new carry exposure")
        order = ([
            ("debit_spread", "Defined-debit directional participation; no new carry campaign"),
            ("target_fly", "Debit-only convexity to a declared level"),
        ] if bias else [
            ("target_fly", "Only with a declared level; otherwise stand aside in cash"),
        ])
        return order, notes

    if lo:
        if bias:
            order = [
                ("target_fly", f"IV {band} ({band_src}) — premium too thin "
                               f"for income; a long fly at your "
                               f"{'upside' if bias > 0 else 'downside'} "
                               f"target is cheap convexity"),
                ("put_bwb" if bias < 0 else "call_bwb",
                 "Thin-credit fallback only if the fly can be entered near "
                 "even money"),
            ]
        else:
            notes.append("IV compressed and no directional view — the "
                         "income book stands aside; deploy in the margin "
                         "account or wait")
            order = [("target_fly", "Only with a level; otherwise no trade")]
        return order, notes

    if hi:
        if skew_rich:
            order = [
                ("otm_put_fly", f"IV {band} + steep put skew (rr25 "
                                f"{rr25:+.1f}v) — sell the richest downside "
                                f"strikes; wider body, smaller size, take "
                                f"profits early"),
                ("put_bwb", "Acceptable, but the broken wing adds tail cash "
                            "use exactly when tails are live — prefer "
                            "symmetric here"),
                ("iron_fly", "Rich straddle, but double cash width in this "
                             "account — third choice"),
            ]
        else:
            order = [
                ("iron_fly", f"IV {band}, flat skew (rr25 {rr25:+.1f}v) — "
                             f"straddle premium is the paid trade; short "
                             f"hold, wide wings"),
                ("balanced_fly", "Same graph in all-puts at half the cash — "
                                 "swap in if put fills are fair"),
                ("otm_put_fly", "Only with a bearish tilt"),
            ]
        notes.append("High-IV entries are IV-crush trades: size down "
                     "25–50% and take profits at the first target")
        return order, notes

    # -- NRM band -------------------------------------------------------------
    if call_bid:
        order = [
            ("call_bwb", f"Call skew bid (rr25 {rr25:+.1f}v — puts UNDER "
                         f"calls): the upside is the expensive side; sell it"),
            ("balanced_fly", "Neutral fallback if the call-side fills are poor"),
            ("put_bwb", "Standard engine is unsubsidised while skew is "
                        "inverted — third choice"),
        ]
        return order, notes

    if skew_rich:
        if bias > 0:
            order = [
                ("m3_bwb_call", f"Mid IV, steep skew (rr25 {rr25:+.1f}v), "
                                f"bullish bias — the ITM call does the call-"
                                f"calendar's job in one expiry"),
                ("put_bwb", "Drop the call if the bias is weak; skew still "
                            "pays the asymmetry"),
                ("call_bwb", "As the upside ADJUSTMENT overlay, not the core"),
            ]
        elif bias < 0:
            order = [
                ("otm_put_fly", f"Mid IV, steep skew, bearish bias — body at "
                                f"−1σ profits from drift into the tent"),
                ("put_bwb", "Same engine, body nearer the money if the "
                            "bearish view is mild"),
                ("target_fly", "Put fly AT the downside target if you have "
                               "a level"),
            ]
        else:
            order = [
                ("put_bwb", f"Mid IV + steep put skew (rr25 {rr25:+.1f}v) + "
                            f"neutral — the default income row; skew pays "
                            f"for the broken wing"),
                ("m3_bwb_call", "Upgrade if a bullish lean develops"),
                ("balanced_fly", "If fills on the broken wing are poor"),
            ]
        return order, notes

    # flat skew, NRM band
    if bias > 0:
        order = [
            ("m3_bwb_call", "Mid IV, flat skew, bullish — BWB carries less "
                            "subsidy but the ITM call supplies the edge"),
            ("balanced_fly", "Neutral carry if the bullish view is weak"),
            ("put_bwb", "Unsubsidised without steep skew — last"),
        ]
    elif bias < 0:
        order = [
            ("otm_put_fly", "Mid IV, flat skew, bearish — body below spot; "
                            "the view, not the skew, is the edge"),
            ("balanced_fly", "If the bearish view is weak"),
            ("target_fly", "Put fly at the level if you have one"),
        ]
    else:
        order = [
            ("balanced_fly", f"Mid IV, flat skew (rr25 {rr25:+.1f}v), "
                             f"neutral — symmetric structures price fairly; "
                             f"all-put version for cash efficiency"),
            ("put_bwb", "Near-equivalent; take it if the broken wing comes "
                        "at even money"),
            ("iron_fly", "Same graph, double the cash width — avoid here"),
        ]
    return order, notes


# ---------------------------------------------------------------- verdict --
def smsf_verdict(ctx: Context, intent: str = "auto") -> dict:
    """Full payload for the SMSF tab, led by ONE action line.

    intent: 'auto' (regime bias decides) | 'bull' | 'neutral' | 'bear'
    """
    reg = ctx.regime
    term = reg.get("term", {})
    band, band_src = vol_band(reg)
    notes: list[str] = []

    if intent == "auto":
        bias = 1 if reg.get("bias", 0) > 0 else -1 if reg.get("bias", 0) < 0 else 0
        notes.append(f"Auto intent: regime bias {reg.get('bias', 0):+d} -> "
                     f"{'bull' if bias > 0 else 'bear' if bias < 0 else 'neutral'}")
    else:
        bias = {"bull": 1, "neutral": 0, "bear": -1}[intent]

    order, g_notes = gate_s(ctx, bias)
    notes += g_notes
    structures = _rank(order)

    vrp_fwd = reg.get("vrp_fwd", 0.0)
    if vrp_fwd < 0:
        notes.append(f"Forward VRP {vrp_fwd:+.1f}v NEGATIVE — short-premium "
                     f"carry is unpaid right now; a sub-target result is the "
                     f"base case. Consider deferring entry")
    if reg.get("vrp_flip"):
        notes.append(f"CAUTION: trailing VRP {reg.get('vrp', 0):+.1f}v "
                     f"disagrees with forward {vrp_fwd:+.1f}v — regime "
                     f"turning, size down")

    stand_aside = (band == "CMP" and bias == 0)
    if stand_aside:
        action = ("ACTION: STAND ASIDE — IV compressed, no directional view; "
                  "no paid single-expiry income structure")
    else:
        action = f"ACTION: {structures[0]['label'].upper()}"
        if term.get("verdict") == "INVERTED FRONT":
            action = ("ACTION: STAND ASIDE — INVERTED FRONT, ZERO NEW CARRY"
                      if bias == 0 else action + " — DEBIT ONLY; ZERO NEW CARRY")

    notes.append("Cash-account doctrine: single expiry only; adjust with "
                 "debit-side adds (long option / debit spread / extra wing) — "
                 "credit-spread adds consume cash equal to their width")

    for g in ctx.gates:
        if g.get("hard"):
            notes.append(f"HARD GATE {g.get('code', '?')}: {g.get('msg', '')}")

    return {
        "symbol": ctx.symbol, "mode": ctx.mode, "intent": intent,
        "bias": bias, "action": action,
        "inputs": {
            "spot": ctx.spot, "iv30": reg.get("iv30"),
            "iv_band": band, "iv_band_src": band_src,
            "iv_pctl": reg.get("iv_pctl"),
            "vrp": reg.get("vrp"), "vrp_fwd": reg.get("vrp_fwd"),
            "har_rv": reg.get("har_rv"),
            "rr25_30d": term.get("rr25_30d"), "skew_rich": term.get("skew_rich"),
            "term": term.get("verdict"), "regime_bias": reg.get("bias"),
            "trend": reg.get("trend"),
        },
        "structures": structures, "notes": notes, "data": ctx.data,
    }


# ------------------------------------------------------ executable v3 -----
_REGISTRY_KEY = {"put_bwb": "bwb"}


def smsf_shortlist(ctx: Context, intent: str = "auto", account: str | None = None,
                   nlv: float | None = None) -> dict:
    """Turn Gate S rankings into exact, risk-evaluated candidates.

    Hypothesis candidates are stage-blocked for live TWS but remain fully
    available for mock campaigns and manual OptionNet Explorer testing.
    """
    from config.loader import account_profile, hypothesis
    from portfolio.governor import evaluate_candidate
    from selection.manage import management_plan
    from strategies import REGISTRY

    base = smsf_verdict(ctx, intent)
    profile = account_profile(account, nlv)
    cards = []
    for row in base["structures"]:
        key = _REGISTRY_KEY.get(row["key"], row["key"])
        strat = REGISTRY.get(key)
        if not strat:
            continue
        props = (strat.propose_for_bias(ctx, base["bias"])
                 if key in ("target_fly", "debit_spread") else strat.propose(ctx))
        for s in props[:2]:
            s.rationale.insert(0, row["why"])
            s.manage = management_plan(ctx, s)
            hyp_id = s.evidence.get("hypothesis_id")
            ev = hypothesis(hyp_id)
            s.evidence = {"hypothesis_id": hyp_id,
                          "status": ev.get("status", s.evidence.get("status", "HYPOTHESIS")),
                          "name": ev.get("name")}
            card = s.to_dict()
            gov = evaluate_candidate(card, ctx.book, profile["nlv"], ctx.spot,
                                     "STAND" if "STAND ASIDE" in base["action"] else
                                     "QUARTER" if ctx.regime.get("vol_state") == "STR" else
                                     "HALF" if ctx.regime.get("vol_state") == "ELV" else "FULL")
            card["governor"] = gov
            card["lots"] = {"lots": gov["approved_lots"],
                            "binding": gov["binding"], "size": gov["size"]}
            card["manual_test_allowed"] = True
            card["tws_stage_allowed"] = (ctx.mode == "mock" or
                                          ev.get("status") in ("PAPER", "ACTIVE"))
            if not card["tws_stage_allowed"]:
                card["blocks"].append("Hypothesis strategy: test in OptionNet/paper before TWS staging")
            cards.append(card)
    cards.sort(key=lambda c: c["score"], reverse=True)
    base.update(cards=cards, account=profile, mode=ctx.mode, spot=ctx.spot,
                policy_id="gate-s-v3", executable=True)
    return base
