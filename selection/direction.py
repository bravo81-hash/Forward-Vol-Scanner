"""Direction module — objective structure selection for a stated intent.

Two gates, both pure functions over an already-built Context (no data
fetching here):

GATE 1 (play type)  — should this be a volatility play at all?
    vrp_fwd >= +3.0v  -> SELL_VOL   (implied rich vs HAR forecast)
    vrp_fwd <= -2.0v  -> BUY_VOL    (implied cheap vs HAR forecast)
    FOMC event premium rich (surface.event_premium) -> EVENT_VOL
    otherwise         -> DELTA      (any edge must come from direction)

GATE 2 (structure matrix) — given a directional intent, rank the four
canonical structures (credit vertical / debit vertical / OTM calendar /
OTM butterfly) from three measurable inputs:
    * IV band       — iv_pctl bands (CMP<25, NRM<60, ELV<85, STR>=85);
                      when no IV history exists (yfinance single names)
                      falls back to the IV30/HAR ratio band, flagged.
    * 25d skew      — term_stats skew_rich (rr25 > 30% of ATM IV)
    * term verdict  — INVERTED FRONT / FLAT / CONTANGO / STEEP CONTANGO

Doctrine matches the rest of the app: ADVISORY, never a block. The matrix
always returns a full ranking with per-structure rationale; Gate 1 speaks
through the play verdict, not by suppressing the ranking.
"""
from __future__ import annotations

from core.models import Context
from core.surface import event_premium

SELL_VRP = 3.0     # vol pts of forward VRP that make selling vol the play
BUY_VRP = -2.0     # vol pts of negative forward VRP that make buying vol the play

# structure keys -> display labels, per side
LABELS = {
    "long": {
        "credit_vertical": "Put credit spread",
        "debit_vertical": "Call debit spread",
        "otm_calendar": "OTM call calendar",
        "otm_butterfly": "OTM call butterfly",
    },
    "short": {
        "credit_vertical": "Call credit spread",
        "debit_vertical": "Put debit spread",
        "otm_calendar": "OTM put calendar",
        "otm_butterfly": "OTM put butterfly",
    },
}

VOL_FAMILIES = {
    "SELL_VOL": ["condor", "bwb", "butterfly"],
    "BUY_VOL": ["calendar", "double_calendar", "diagonal"],
    "EVENT_VOL": ["calendar (event variant — sell first post-FOMC expiry)"],
}


# ------------------------------------------------------------------ bands ---
def vol_band(reg: dict) -> tuple[str, str]:
    """(band, source). Band from IV rank when history exists; otherwise a
    documented proxy from IV30 vs the HAR realized-vol forecast."""
    if reg.get("ivp_proxy"):
        har = reg.get("har_rv") or 0.0
        iv30 = reg.get("iv30") or 0.0
        ratio = iv30 / har if har else 1.0
        band = "ELV" if ratio >= 1.20 else "CMP" if ratio <= 0.90 else "NRM"
        return band, f"proxy: IV30/HAR = {ratio:.2f} (no IV history)"
    return reg.get("vol_state", "NRM"), f"IV rank {reg.get('iv_pctl', 50.0):.0f}%ile"


# ----------------------------------------------------------------- gate 1 ---
def gate1(ctx: Context) -> dict:
    reg = ctx.regime
    why: list[str] = []
    term = reg.get("term", {})

    ev = None
    try:
        ev = event_premium(ctx.slices, ctx.today, reg.get("rv21", 0.0))
    except Exception:
        ev = None

    vrp_fwd = reg.get("vrp_fwd", 0.0)
    if ev and ev.get("rich") and term.get("verdict") == "INVERTED FRONT":
        play = "EVENT_VOL"
        why.append(f"FOMC event premium rich — implied event move "
                   f"{ev['implied_move_pct']:.2f}% vs ~0.9% historical; front inverted")
    elif vrp_fwd >= SELL_VRP:
        play = "SELL_VOL"
        why.append(f"Forward VRP {vrp_fwd:+.1f}v (IV30 {reg['iv30']:.1f} vs HAR "
                   f"{reg['har_rv']:.1f}) — implied rich, selling vol is paid")
    elif vrp_fwd <= BUY_VRP:
        play = "BUY_VOL"
        why.append(f"Forward VRP {vrp_fwd:+.1f}v — implied cheap vs HAR forecast, "
                   f"long-vol structures are subsidised")
    else:
        play = "DELTA"
        why.append(f"Forward VRP {vrp_fwd:+.1f}v is unremarkable "
                   f"(|{BUY_VRP}| .. +{SELL_VRP} band) — no standalone vol edge; "
                   f"edge must come from direction")
    if reg.get("vrp_flip"):
        why.append(f"CAUTION: trailing VRP {reg.get('vrp', 0):+.1f}v disagrees with "
                   f"forward {vrp_fwd:+.1f}v — regime is turning, size down")
    return {"play": play, "why": why,
            "event": ({"implied_move_pct": ev["implied_move_pct"], "rich": ev["rich"]}
                      if ev else None)}


# ----------------------------------------------------------------- gate 2 ---
def _rank(side: str, order: list[tuple[str, str]]) -> list[dict]:
    return [{"rank": i + 1, "key": k, "label": LABELS[side][k], "why": w}
            for i, (k, w) in enumerate(order)]


def gate2(ctx: Context, side: str) -> list[dict]:
    """Ranked structures for a directional intent. side: 'long' | 'short'."""
    reg = ctx.regime
    term = reg.get("term", {})
    band, band_src = vol_band(reg)
    skew_rich = bool(term.get("skew_rich"))
    rr25 = term.get("rr25_30d", 0.0)
    tverd = term.get("verdict", "FLAT")
    front_rich = tverd in ("INVERTED FRONT", "FLAT")
    vrp_fwd = reg.get("vrp_fwd", 0.0)

    hi, lo = band in ("ELV", "STR"), band == "CMP"

    if side == "long":
        if hi:
            skew_note = (f"and collects steep put skew (rr25 {rr25:+.1f}v)"
                         if skew_rich else
                         f"but skew is normal (rr25 {rr25:+.1f}v) — credit thinner")
            order = [
                ("credit_vertical", f"IV {band} — short rich vega {skew_note}"),
                ("otm_butterfly", "Short vega with cheap convexity to a defined "
                                  "upside target; pays if RV < implied path"),
                ("debit_vertical", f"Fights the vega: paying {band} IV for the "
                                   f"long wing — only with a fast-move thesis"),
            ]
        elif lo:
            cal_why = ("Front IV rich vs back — harvest front decay while long "
                       "back-month vega + upside delta"
                       if front_rich else
                       "Term in contango — back month not subsidised; calendar "
                       "edge weaker here")
            order = [
                ("debit_vertical", f"IV {band} ({band_src}) — long cheap vega; "
                                   f"upside wing is inexpensive"),
                ("otm_calendar", cal_why),
                ("credit_vertical", "Credit is thin at compressed IV — poor pay "
                                    "for the tail risk"),
            ]
            if not front_rich:  # contango: vertical clearly first, calendar last
                order[1], order[2] = order[2], order[1]
        else:  # NRM
            if tverd == "INVERTED FRONT":
                order = [
                    ("otm_calendar", "Front inverted — sell rich front decay, own "
                                     "back vega, positive delta from OTM call strike"),
                    ("credit_vertical" if skew_rich else "debit_vertical",
                     "Skew steep — sell the rich put wing" if skew_rich
                     else "Skew normal — buying the call wing costs fair value"),
                    ("otm_butterfly", "Target play if you have a level"),
                ]
            elif skew_rich:
                order = [
                    ("credit_vertical", f"Mid IV but put skew steep (rr25 "
                                        f"{rr25:+.1f}v) — the put wing is the "
                                        f"overpriced side; sell it"),
                    ("debit_vertical", "Acceptable; call wing fairly priced"),
                    ("otm_butterfly", "Only with a specific upside target"),
                ]
            else:
                tie = ("credit_vertical" if vrp_fwd > 0 else "debit_vertical")
                alt = ("debit_vertical" if tie == "credit_vertical"
                       else "credit_vertical")
                order = [
                    (tie, f"Mid IV, normal skew — forward VRP {vrp_fwd:+.1f}v "
                          f"tiebreak favours the {'credit' if vrp_fwd > 0 else 'debit'} side"),
                    (alt, "Near-equivalent here; pick by strike liquidity"),
                    ("otm_butterfly", "Only with a specific upside target"),
                ]
    else:  # short delta — mirror, with the index put-skew asymmetry made explicit
        if hi:
            if skew_rich:
                order = [
                    ("otm_butterfly", f"IV {band} + steep put skew (rr25 "
                                      f"{rr25:+.1f}v) — the fly sells the richest "
                                      f"downside strikes; short vega, defined target"),
                    ("credit_vertical", "Short rich vega, but the call wing is the "
                                        "CHEAP side under put skew — credit thin"),
                    ("debit_vertical", "Long put is skew-taxed, but the short lower "
                                       "put is even richer — spread is part-subsidised"),
                ]
            else:
                order = [
                    ("credit_vertical", f"IV {band} — short rich vega; skew normal "
                                        f"so the call credit is fairly paid"),
                    ("otm_butterfly", "Short vega, cheap convexity to a downside target"),
                    ("debit_vertical", f"Paying {band} IV for the long put — only "
                                       f"with a fast-move thesis"),
                ]
        elif lo:
            cal_why = ("Front IV rich vs back — harvest front decay while long "
                       "back-month vega + downside delta"
                       if front_rich else
                       "Term in contango — calendar edge weaker here")
            order = [
                ("debit_vertical", f"IV {band} ({band_src}) — long cheap vega; the "
                                   f"short lower put offsets the skew cost of the long put"),
                ("otm_calendar", cal_why),
                ("credit_vertical", "Call credit is doubly thin: low IV and the "
                                    "cheap side of skew"),
            ]
            if not front_rich:
                order[1], order[2] = order[2], order[1]
        else:  # NRM
            if tverd == "INVERTED FRONT":
                order = [
                    ("otm_calendar", "Front inverted — sell rich front decay, own "
                                     "back vega, negative delta from OTM put strike"),
                    ("debit_vertical" if skew_rich else "credit_vertical",
                     "Skew steep — put debit spread is part-subsidised by the "
                     "richer short lower put" if skew_rich
                     else "Skew normal — call credit fairly paid"),
                    ("otm_butterfly", "Target play if you have a level"),
                ]
            elif skew_rich:
                order = [
                    ("debit_vertical", f"Put skew steep (rr25 {rr25:+.1f}v): the "
                                       f"short lower put is richer than the long put "
                                       f"— skew subsidises the debit spread"),
                    ("otm_butterfly", "Sells the rich downside strikes with defined risk"),
                    ("credit_vertical", "Call wing is the cheap side of skew — thin credit"),
                ]
            else:
                tie = ("credit_vertical" if vrp_fwd > 0 else "debit_vertical")
                alt = ("debit_vertical" if tie == "credit_vertical"
                       else "credit_vertical")
                order = [
                    (tie, f"Mid IV, normal skew — forward VRP {vrp_fwd:+.1f}v "
                          f"tiebreak favours the {'credit' if vrp_fwd > 0 else 'debit'} side"),
                    (alt, "Near-equivalent here; pick by strike liquidity"),
                    ("otm_butterfly", "Only with a specific downside target"),
                ]
    return _rank(side, order)


# ---------------------------------------------------------------- verdict ---
def direction_verdict(ctx: Context, intent: str = "auto") -> dict:
    """Full payload for the Direction tab, led by ONE unambiguous action line.

    intent: 'long' | 'short' | 'vol' | 'auto'

    Behaviour by intent:
      auto  — "tell me the trade": ONE verdict only. If Gate 1 says a vol
              play pays, recommend the vol family (run Scan for cards) and
              SUPPRESS the directional ranking. If Gate 1 says DELTA,
              resolve the side from regime bias and rank structures.
              Bias 0 + no vol edge -> stand aside.
      long/short — "I have a view, give me the structure": ranking always
              shown (the view is respected as an input); a one-line flag
              is added when Gate 1 says vol is the better-paid trade.
      vol   — Gate-1 verdict mapped to the strategy families.
    """
    reg = ctx.regime
    term = reg.get("term", {})
    band, band_src = vol_band(reg)
    g1 = gate1(ctx)
    play, is_vol = g1["play"], g1["play"] != "DELTA"
    notes: list[str] = []

    side = None
    if intent in ("long", "short"):
        side = intent
    elif intent == "auto" and not is_vol:
        bias = reg.get("bias", 0)
        if bias > 0:
            side = "long"
            notes.append(f"Auto intent: regime bias {bias:+d} -> long delta")
        elif bias < 0:
            side = "short"
            notes.append(f"Auto intent: regime bias {bias:+d} -> short delta")

    show_structures = intent in ("long", "short") or (intent == "auto" and
                                                      side is not None)
    structures = gate2(ctx, side) if (show_structures and side) else []

    fams = " / ".join(VOL_FAMILIES.get(play, []))
    if intent in ("long", "short"):
        action = f"ACTION: {structures[0]['label'].upper()}"
        if is_vol:
            action += (f" — but {play.replace('_', ' ')} is the better-paid "
                       f"trade here ({fams}; run Scan)")
            notes.append("Gate 1 says the paid trade is a VOL play — the "
                         "directional ranking is best-available for your "
                         "stated view, not the best idea on the tape")
    elif intent == "vol":
        action = (f"ACTION: {play.replace('_', ' ')} — {fams}; run Scan for cards"
                  if is_vol else
                  "ACTION: NO VOL EDGE — forward VRP flat; express a "
                  "directional view or stand aside")
    else:  # auto
        if is_vol:
            action = f"ACTION: {play.replace('_', ' ')} — {fams}; run Scan for cards"
        elif side:
            action = f"ACTION: {structures[0]['label'].upper()}"
        else:
            action = ("ACTION: STAND ASIDE — no vol edge and no directional "
                      "conviction (regime bias 0)")
            notes.append("Auto intent: regime bias 0 — pick a side yourself "
                         "(Long/Short) if you have a view the regime doesn't")

    for g in ctx.gates:
        if g.get("hard"):
            notes.append(f"HARD GATE {g.get('code', '?')}: {g.get('msg', '')}")

    return {
        "symbol": ctx.symbol, "mode": ctx.mode, "intent": intent,
        "side": side, "play": play, "action": action, "play_why": g1["why"],
        "event": g1["event"],
        "inputs": {
            "spot": ctx.spot, "iv30": reg.get("iv30"),
            "iv_band": band, "iv_band_src": band_src,
            "iv_pctl": reg.get("iv_pctl"),
            "vrp": reg.get("vrp"), "vrp_fwd": reg.get("vrp_fwd"),
            "har_rv": reg.get("har_rv"), "rv21": reg.get("rv21"),
            "rr25_30d": term.get("rr25_30d"), "skew_rich": term.get("skew_rich"),
            "term": term.get("verdict"), "bias": reg.get("bias"),
            "trend": reg.get("trend"),
        },
        "structures": structures, "notes": notes, "data": ctx.data,
    }
