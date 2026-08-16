"""Card renderer for the equity LEAPS engine.

Format rule (docs/equity_leaps.md §5): every bullet is a complete sentence that
states the value AND what it implies, with the number embedded rather than
prefixed as a label. A bare figure makes the reader interpret it twice — once
on reading, and again when trying to remember why it mattered.

    Wrong:  IV rank: 31
    Right:  IV rank is 31, sitting in the bottom third of the past year, so
            premium is cheap here and long-vega structures are favoured.

Blocked cards state the gate that failed and the condition that would clear it,
because "no trade" without a reason is indistinguishable from a bug.
"""
from __future__ import annotations

import re

SECTIONS = ("SETUP", "STRUCTURE", "ENTRY", "EXIT", "MANAGE", "FLAGS")
MAX_SENTENCES = 2

# A full stop inside 25.0% or $1,234.56 is not a sentence boundary, so only
# count terminators followed by whitespace or end-of-string.
_TERMINATOR = re.compile(r"[.!?](?=\s|$)")


def _sentences(text: str) -> int:
    return len(_TERMINATOR.findall(text.strip()))


def validate(card: dict) -> list[str]:
    """Format violations. Used by tests to keep the renderer honest."""
    problems = []
    for section in SECTIONS:
        for bullet in card.get("sections", {}).get(section, []):
            if not bullet.strip().endswith((".", "!", "?")):
                problems.append(f"{section}: bullet does not end in a full stop: {bullet[:60]}")
            if _sentences(bullet) > MAX_SENTENCES:
                problems.append(f"{section}: bullet exceeds {MAX_SENTENCES} sentences: {bullet[:60]}")
            # FLAGS legitimately lead with a block code (TREND BLOCK: ...).
            if section != "FLAGS" and ":" in bullet.split(" ")[0]:
                problems.append(f"{section}: bullet uses a label prefix: {bullet[:60]}")
    return problems


def _header(payload: dict) -> dict:
    i = payload.get("inputs", {})
    bits = [payload.get("stage", "?")]
    if i.get("iv_rank") is not None:
        bits.append(f"IV rank {i['iv_rank']:.0f}")
    elif i.get("iv_rank_proxy"):
        bits.append("IV rank n/a")
    if i.get("iv_rv") is not None:
        bits.append(f"IV/RV {i['iv_rv']:.2f}")
    if i.get("skew_rr25") is not None:
        bits.append(f"Skew {i['skew_rr25']:+.1f}")
    return {"symbol": payload.get("symbol"), "spot": payload.get("spot"),
            "meta": " · ".join(bits)}


def render(gate_e: dict, *, radar: dict | None = None,
           trigger: dict | None = None,
           governance: dict | None = None) -> dict:
    """Assemble the card from a Gate E payload plus optional context."""
    sections = {s: [] for s in SECTIONS}

    # -------------------------------------------------------------- SETUP
    if radar:
        sections["SETUP"] += list(radar.get("reasons", []))
    if trigger:
        sections["SETUP"] += list(trigger.get("checks", []))
    if not sections["SETUP"]:
        i = gate_e.get("inputs", {})
        sections["SETUP"].append(
            f"{gate_e['symbol']} is trading at ${gate_e['spot']:.2f} and reads as "
            f"{gate_e.get('stage', 'an indeterminate stage')} on the weekly "
            f"structure, which is what gates the direction of any structure below.")
        if i.get("iv_rv") is not None:
            sections["SETUP"].append(
                f"Implied volatility sits at {i['iv30']:.1f}% against realised of "
                f"{i['rv21']:.1f}%, a ratio of {i['iv_rv']:.2f}, which decides whether "
                f"this is a name to buy premium on or sell it on.")

    # ---------------------------------------------------------- STRUCTURE
    suggestions = gate_e.get("suggestions", [])
    if not gate_e.get("eligible"):
        sections["STRUCTURE"].append(
            "No structure is recommended, because at least one hard gate rejected "
            "the candidate before the selection matrix was reached.")
    elif not suggestions:
        sections["STRUCTURE"].append(
            f"The selection matrix chose {', '.join(gate_e['structures'])} but no "
            f"concrete legs could be built from the current chain, so nothing is "
            f"staged rather than showing a structure with partial data.")
    else:
        top = suggestions[0]
        sections["STRUCTURE"].append(
            f"The recommended structure is {top['label']}, built as "
            f"{' / '.join(top['legs'])}.")
        delta = (top.get("greeks") or {}).get("delta")
        greek_text = (f", and net delta is {delta:.0f}" if delta is not None
                      else ", while net delta is unavailable")
        sections["STRUCTURE"].append(
            f"Net {'debit' if top['net_mid'] > 0 else 'credit'} is "
            f"${abs(top['net_mid']) * 100:,.0f} per spread with maximum loss of "
            f"${abs(top['max_loss']) * 100:,.0f}{greek_text}.")
        greeks = top.get("greeks") or {}
        if greeks.get("theta") is not None and greeks.get("vega") is not None:
            sections["STRUCTURE"].append(
                f"Computed theta is {greeks['theta']:+.2f} and computed vega is "
                f"{greeks['vega']:+.2f}, so neither exposure is being assumed away.")
        provenance = top.get("price_provenance") or []
        if provenance:
            sections["STRUCTURE"].append(
                "Leg price provenance is " + "; ".join(provenance) + ".")
        if top.get("breakevens"):
            be = top["breakevens"][0]
            sections["STRUCTURE"].append(
                f"Breakeven at expiry is ${be:.2f} against spot of ${gate_e['spot']:.2f}, "
                f"a difference of {(be / gate_e['spot'] - 1) * 100:+.1f}%, which is the "
                f"honest measure of what the structure costs before it works.")
        sections["STRUCTURE"] += list(top.get("rationale", []))[:3]

        # ---------------------------------------------------------- ENTRY
        sections["ENTRY"].append(
            "Fill the long leg first at the mid and work the order, because the "
            "deepest in-the-money strike carries the widest quote and paying the "
            "offer there is the largest avoidable cost in the trade.")
        if any(l.startswith("-") for l in top["legs"]):
            sections["ENTRY"].append(
                "Sell the short leg into strength once the long leg is filled, "
                "then re-verify the headroom rule against your actual fills "
                "rather than the model prices shown here.")
        if trigger and trigger.get("level"):
            sections["ENTRY"].append(
                f"The trigger level is ${trigger['level']:.2f}, and entry is only "
                f"valid while price holds above it on a closing basis.")

        # ----------------------------------------------------------- EXIT
        if top.get("max_profit_unbounded"):
            sections["EXIT"].append(
                "The payoff is unbounded above the short strike, so take profit "
                "against a return-on-debit target set at entry rather than a fictitious "
                "percentage of maximum value.")
        else:
            sections["EXIT"].append(
                "Take profit at 60% of the bounded maximum modelled value, before "
                "the nearest short leg expires.")
        if trigger and trigger.get("level"):
            sections["EXIT"].append(
                f"Stop out on a decisive close back below ${trigger['level']:.2f}, and "
                f"close the whole structure rather than keeping an orphaned long "
                f"leg to give it room.")
        else:
            sections["EXIT"].append(
                "Stop out on a decisive close below the level recorded at entry, "
                "using that fixed level rather than a recomputed moving average, "
                "so the stop cannot quietly widen while the position is open.")

        # --------------------------------------------------------- MANAGE
        manage = top.get("manage") or {}
        for value in manage.values():
            if isinstance(value, str) and value.strip():
                sections["MANAGE"].append(value if value.endswith(".") else value + ".")
        if not sections["MANAGE"]:
            sections["MANAGE"].append(
                "Roll any short leg at 21 days to expiry or once it decays to "
                "10-15% of the premium collected, whichever comes first.")
        sections["MANAGE"].append(
            "Log the fills back into the app on entry and on close, because the "
            "shadow-mode record is what decides whether this engine has an edge "
            "at all.")

    # -------------------------------------------------------------- FLAGS
    sections["FLAGS"] += list(gate_e.get("blocks", []))
    sections["FLAGS"] += list(gate_e.get("notes", []))
    if governance and governance.get("checked") is True:
        sections["FLAGS"] += list(governance.get("reasons", []))
    else:
        sections["FLAGS"].append(
            "Portfolio governance was not checked, so the card makes no claim about "
            "delta budget, correlation, account routing or margin capacity.")
    if not sections["FLAGS"]:
        sections["FLAGS"].append(
            "No additional flags were raised by the checks that actually ran.")

    return {"header": _header(gate_e),
            "action": gate_e.get("action", "STAND ASIDE"),
            "eligible": gate_e.get("eligible", False),
            "sections": {k: v for k, v in sections.items() if v},
            "policy_id": gate_e.get("policy_id")}


def to_text(card: dict) -> str:
    h = card["header"]
    lines = [f"{h['symbol']} — ${h['spot']:.2f}", h["meta"], "─" * 66,
             f"ACTION: {card['action']}", ""]
    for section in SECTIONS:
        bullets = card["sections"].get(section)
        if not bullets:
            continue
        lines.append(section)
        for b in bullets:
            lines.append(f"· {b}")
        lines.append("")
    return "\n".join(lines)
