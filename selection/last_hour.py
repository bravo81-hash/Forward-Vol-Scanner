"""Focused 15:00-15:40 ET decision engine.

This selector intentionally ignores the broad research registry.  It returns
only the user's three regime flies, standard TimeEdge, and paper-gated
TimeZone, with one visible reason for every ENTER/WAIT/LOCKED verdict.
"""
from __future__ import annotations

from datetime import date

from core.models import Leg
from core.pricing import q_for, risk_profile
from execution.optionstrat import optionstrat_url
from portfolio.governor import evaluate_candidate
from strategies.last_hour import LAST_HOUR_REGISTRY

MULTI_EXPIRY = {"timeedge", "timezone"}

PLAYBOOK = {
    "fly_bull": {
        "title": "Bullish pullback fly", "regime": "BULLISH",
        "pt": 0.09, "sl": 0.07, "hard": 0.10, "hold": 10, "exit_dte": 14,
        "adjust": [
            "If a rally turns position delta negative: close or move the upper long once to restore positive delta.",
            "If spot closes below the short body: one full reset down only when >14 DTE and loss <5%.",
            "Second structural breach: exit. No calendar overlay.",
        ],
    },
    "fly_chop": {
        "title": "Chop / range fly", "regime": "CHOP",
        "pt": 0.06, "sl": 0.07, "hard": 0.09, "hold": 7, "exit_dte": None,
        "adjust": [
            "Adjust only when spot is outside a long strike AND |position delta| exceeds 50/lot.",
            "With >7 DTE and loss <5%: recenter the entire fly once; preserve widths and risk.",
            "At ≤7 DTE, after a second breach, or after the stop: exit.",
        ],
    },
    "fly_bear": {
        "title": "Bearish target fly", "regime": "BEARISH",
        "pt": 0.11, "sl": 0.075, "hard": 0.10, "hold": 10, "exit_dte": 14,
        "adjust": [
            "Close above the failed-bounce resistance or at the stop; do not roll the thesis upward.",
            "Take the trade at the short body once ≥8% is earned; close through the lower long.",
            "A lower re-entry is a new trade, not an adjustment.",
        ],
    },
    "timeedge": {
        "title": "TimeEdge", "regime": "CALM / RANGE",
        "pt": 0.10, "sl": 0.10, "hard": 0.10, "hold": 7, "exit_dte": 7,
        "adjust": [
            "One predefined recenter maximum only after a T+N breakeven or 0.75–1.0 expected-move breach.",
            "Reprice exact strike IVs before the roll; do not add an automatic second tent.",
            "Next breach, −10%, scheduled-event exit, or 7 front DTE: close.",
        ],
    },
    "timezone": {
        "title": "TimeZone", "regime": "RUT CALM / RANGE",
        "pt": 0.05, "sl": 0.05, "hard": 0.05, "hold": 7, "exit_dte": 7,
        "adjust": [
            "One defense only: reduce or close the threatened 20-point put credit spread first.",
            "Do not add new downside complexity to rescue the PCS.",
            "After that defense, the next stop or structural breach closes the whole position.",
        ],
    },
}


def _major_event(ctx, days: int) -> bool:
    ev = ctx.events or {}
    return min(int(ev.get("fomc_dte", 999)), int(ev.get("macro_dte", 999))) <= days


def _fit(key: str, ctx, profile: dict, *, active_time_spread: str,
         te_completed: int, tz_paper: int, trigger: str) -> tuple[float, list[str], list[str]]:
    r, term = ctx.regime, ctx.regime.get("term", {})
    trend, adx = r.get("trend", "RNG"), float(r.get("adx") or 0)
    band, vrp = r.get("vol_state", "NRM"), float(r.get("vrp_fwd") or 0)
    verdict = term.get("verdict", "FLAT")
    bias = int(r.get("bias") or 0)
    ok, wait = [], []
    score = 25.0

    if verdict == "INVERTED FRONT":
        wait.append("Front term structure is inverted: no new desk entry.")
    if band == "STR" or abs(float(r.get("iv_chg_pct") or 0)) > 12:
        wait.append("Volatility is stressed or still repricing: wait for the surface to settle.")
    if profile.get("block_multi_expiry") and key in MULTI_EXPIRY:
        wait.append("Cash/SMSF mandate blocks multi-expiry positions.")

    if key == "fly_bull":
        if trend == "UP" or bias > 0:
            score += 50
            ok.append("Bullish structure is intact.")
        else:
            wait.append("Needs an uptrend or positive bias.")
        if vrp > 0:
            score += 12
            ok.append(f"Forward VRP is paid at {vrp:+.1f}v.")
        else:
            wait.append("Forward VRP is not positive.")
        if trigger == "bull_pullback":
            score += 12
            ok.append("Controlled red-day pullback is confirmed.")
        else:
            wait.append("Confirm a controlled red-day pullback with no nearby resistance.")
    elif key == "fly_chop":
        if trend == "RNG" and adx < 22:
            score += 55
            ok.append(f"Range regime confirmed; ADX {adx:.1f}.")
        else:
            wait.append(f"Needs range structure and ADX <22; now {trend}, ADX {adx:.1f}.")
        if vrp > 0:
            score += 10
            ok.append(f"IV exceeds forecast RV by {vrp:.1f}v.")
        else:
            wait.append("Do not sell the range when forecast RV exceeds IV.")
    elif key == "fly_bear":
        if trend == "DN" or bias < 0:
            score += 50
            ok.append("Bearish structure is intact.")
        else:
            wait.append("Needs a downtrend or negative bias.")
        if trigger == "bear_failed_bounce":
            score += 12
            ok.append("Failed bounce near resistance is confirmed.")
        else:
            wait.append("Confirm a failed bounce near resistance, not an extended breakdown candle.")
        if band in ("NRM", "ELV"):
            score += 10
            ok.append("IV is usable for a defined-debit target fly.")
    elif key == "timeedge":
        if ctx.symbol != "SPX":
            wait.append("TimeEdge is SPX only.")
        if trend == "RNG" and adx < 22:
            score += 40
            ok.append(f"SPX is calm/range; ADX {adx:.1f}.")
        else:
            wait.append("TimeEdge needs calm SPX range conditions.")
        iv = float(r.get("iv30") or 0)
        if 14 <= iv <= 22:
            score += 20
            ok.append(f"IV {iv:.1f} is inside the 14–22 calendar zone.")
        else:
            wait.append(f"IV {iv:.1f} is outside the 14–22 TimeEdge zone.")
        if _major_event(ctx, 7):
            wait.append("Tier-1 event is inside the intended hold; skip or use its mandatory pre-event exit.")
        if active_time_spread == "timezone":
            wait.append("TimeZone is already active; initially alternate rather than overlap.")
    elif key == "timezone":
        if ctx.symbol != "RUT":
            wait.append("TimeZone is RUT only.")
        if te_completed < 3 or tz_paper < 3:
            wait.append(f"Progression lock: {te_completed}/3 rule-perfect TimeEdge and {tz_paper}/3 clean TimeZone simulations.")
        if active_time_spread == "timeedge":
            wait.append("TimeEdge is open; do not overlap TimeZone.")
        if trend == "RNG" and adx < 22:
            score += 35
            ok.append(f"RUT is calm/range; ADX {adx:.1f}.")
        else:
            wait.append("TimeZone needs calm RUT range conditions.")
        iv = float(r.get("iv30") or 0)
        if iv < 24:
            score += 15
            ok.append(f"RUT IV proxy {iv:.1f} is below 24.")
        else:
            wait.append(f"RUT IV proxy {iv:.1f} is too high; avoid above 28 and prefer below 24.")
        if verdict in ("CONTANGO", "STEEP CONTANGO"):
            score += 10
            ok.append("Term structure is in contango.")
        else:
            wait.append("TimeZone requires contango.")
        if _major_event(ctx, 7):
            wait.append("Major scheduled event is inside the intended hold.")
    return score, ok, wait


def _risk_plan(key: str, card: dict, ctx) -> dict:
    rule = PLAYBOOK[key]
    structural = abs(float(card.get("max_loss") or 0)) * 100
    debit = max(float(card.get("net_mid") or 0), 0) * 100
    planned = debit if key == "timeedge" else max(structural, debit, 1.0)
    if key == "timezone":
        planned = max(structural, abs(float(card.get("net_mid") or 0)) * 100, 1.0)
    pt = round(planned * rule["pt"], 0)
    sl = round(-planned * rule["sl"], 0)
    exit_dte = rule["exit_dte"]
    if key == "fly_chop":
        exit_dte = 2 if ctx.symbol == "SPX" else 7
    return {
        "planned_capital": round(planned, 0),
        "pt_pct": round(rule["pt"] * 100, 1), "sl_pct": round(rule["sl"] * 100, 1),
        "hard_sl_pct": round(rule["hard"] * 100, 1),
        "pt_dollars": pt, "sl_dollars": sl,
        "pt_note": f"+{rule['pt']*100:g}% planned capital",
        "sl_note": f"-{rule['sl']*100:g}% planned capital",
        "time_stop": {"rule": f"max {rule['hold']} trading days; exit before {exit_dte} DTE"},
        "adjustments": rule["adjust"],
    }


def attach_trade_tools(card: dict, ctx) -> None:
    """Attach one canonical OptionStrat link and the repriced risk profile."""
    legs = [Leg(cp=leg["cp"], strike=float(leg["strike"]),
                expiry=date.fromisoformat(leg["expiry"]), qty=int(leg["qty"]),
                iv=float(leg.get("iv") or 0.18))
            for leg in card["legs_raw"]]
    card["optionstrat_url"] = optionstrat_url(ctx.symbol, card["legs_raw"])
    card["risk_profile"] = risk_profile(
        ctx.spot, legs, ctx.today, entry=float(card["net_mid"]),
        q=float(getattr(ctx, "q", q_for(ctx.symbol))))
    # The serialized IVs and displayed cent-rounded entry are the executable
    # model inputs. Keep every downstream risk number on that exact profile.
    profile = card["risk_profile"]
    card["max_profit"] = profile["max_profit"]
    card["max_loss"] = profile["max_loss"]
    card["breakevens"] = profile["breakevens"]
    card["cash_required"] = round(abs(profile["max_loss"]) * 100, 2)


def last_hour_decision(ctx, profile: dict, *, preferred: str = "auto",
                       active_time_spread: str = "none", te_completed: int = 0,
                       tz_paper: int = 0, trigger: str = "none") -> dict:
    cards, excluded = [], []
    global_stop = (ctx.regime.get("term", {}).get("verdict") == "INVERTED FRONT" or
                   ctx.regime.get("vol_state") == "STR" or
                   abs(float(ctx.regime.get("iv_chg_pct") or 0)) > 12)
    size = "HALF" if ctx.symbol == "RUT" or ctx.regime.get("vol_state") == "ELV" else "FULL"

    for key, strat in LAST_HOUR_REGISTRY.items():
        props = strat.propose(ctx)
        if not props:
            excluded.append({"strategy": key, "reason": f"Not constructible for {ctx.symbol} today."})
            continue
        s = props[0]
        score, reasons, waits = _fit(
            key, ctx, profile, active_time_spread=active_time_spread,
            te_completed=te_completed, tz_paper=tz_paper, trigger=trigger)
        if key == "timeedge":
            fiv, biv = s.legs[0].iv * 100, s.legs[1].iv * 100
            if biv - fiv > 1.0:
                waits.append(f"Back IV is {biv-fiv:.2f}v above front at the exact strike; maximum is +1.0v.")
        if key == "timezone" and float(s.evidence.get("pcs_credit") or 0) <= 1.50:
            waits.append(f"Model PCS credit {s.evidence.get('pcs_credit', 0):.2f} does not clear $1.50.")
        card = s.to_dict()
        eligible = not waits and not global_stop
        card.update(
            fit_score=round(score, 1), status="ENTER" if eligible else "WAIT",
            entry_reasons=reasons, wait_reasons=waits, permitted=eligible,
            edge_tier="PLAYBOOK" if eligible else "NOT READY",
            tws_stage_allowed=eligible,
            blocks=[] if eligible else waits,
            rank_reason=(reasons[0] if eligible and reasons else waits[0] if waits else "No edge"),
            policy_id="last-hour-v1",
        )
        attach_trade_tools(card, ctx)
        card["manage"] = _risk_plan(key, card, ctx)
        gov = evaluate_candidate(card, ctx.book, profile["nlv"], ctx.spot, size)
        card["governor"] = gov
        card["lots"] = {"lots": gov["approved_lots"], "binding": gov["binding"], "size": size}
        if eligible and not gov["risk_approved"]:
            card["status"] = "WAIT"
            card["permitted"] = card["tws_stage_allowed"] = False
            card["wait_reasons"].append("Portfolio governor approves zero additional lots.")
            card["blocks"].append("portfolio governor approves zero lots")
        cards.append(card)

    cards.sort(key=lambda c: (c["permitted"], c["fit_score"]), reverse=True)
    if preferred != "auto":
        cards.sort(key=lambda c: (c["strategy"] == preferred, c["permitted"], c["fit_score"]), reverse=True)
    primary = next((c for c in cards if c["permitted"] and
                    (preferred == "auto" or c["strategy"] == preferred)), None)
    for i, card in enumerate(cards, 1):
        card["rank"] = i
    r, term = ctx.regime, ctx.regime.get("term", {})
    action = (f"ENTER {primary['label'].upper()}" if primary else
              "NO NEW TRADE — MANAGE RISK / WAIT")
    return {
        "symbol": ctx.symbol, "spot": ctx.spot, "mode": ctx.mode,
        "policy_id": "last-hour-v1", "action": action,
        "primary_strategy": primary["strategy"] if primary else None,
        "account": profile, "data": ctx.data, "book": ctx.book,
        "market": {
            "trend": r.get("trend"), "bias": r.get("bias"), "adx": r.get("adx"),
            "iv30": r.get("iv30"), "iv_pctl": r.get("iv_pctl"), "rv21": r.get("rv21"),
            "vrp_fwd": r.get("vrp_fwd"), "iv_chg_pct": r.get("iv_chg_pct"),
            "term": term.get("verdict"), "rr25": term.get("rr25_30d"),
            "skew_rich": term.get("skew_rich"), "event": ctx.events,
        },
        "progression": {
            "active_time_spread": active_time_spread,
            "te_completed": te_completed, "timezone_paper": tz_paper,
            "timezone_unlocked": te_completed >= 3 and tz_paper >= 3,
            "rule": "Start with TimeEdge. Complete three rule-perfect campaigns and three clean TimeZone simulations; initially alternate, never overlap.",
        },
        "workflow": [
            {"time": "15:00", "task": "Refresh TWS; verify New York session, data freshness and account."},
            {"time": "15:08", "task": "Read bias, ADX, IV–RV, term structure, skew and event gate."},
            {"time": "15:18", "task": "Open the primary card; verify ONE/TWS risk graph and exact-strike IVs."},
            {"time": "15:30", "task": "Stage one limit order untransmitted; review combo, margin and quantity."},
            {"time": "15:40", "task": "No new entries. Record campaign, PT, SL, time stop and one allowed defense."},
        ],
        "cards": cards, "excluded": excluded,
        "notes": [
            "Only five playbook structures are considered; broad research families are intentionally hidden.",
            "All staging remains transmit=False for manual TWS review.",
            "SPX and RUT share one equity-volatility risk bucket.",
        ],
    }
