"""Live repricing of suggestion cards from the actual option chain.

Model prices come from skew-interpolated BS and drift from reality,
especially on deep-OTM wings (BWB lower longs). In live mode the exact
legs are re-quoted: net_mid becomes the NBBO mid, greeks become TWS
modelGreeks (same units as core.pricing: theta/day, vega per vol pt),
and max P/L + breakevens are recomputed against the live entry. The
model number is kept in model_mid for comparison. Cards whose legs
have no two-sided quote keep model values (mid_src stays "model").

TWS budget: one qualify + one batched quote pass over the unique legs
of the shortlist (<= ~14 lines).
"""
from __future__ import annotations

from datetime import date

from .chain import SURFACE_CFG
from .ib_client import quote_many
from .models import Leg
from .pricing import MULT, q_for, struct_metrics

GREEK_KEYS = ("delta", "gamma", "theta", "vega")
WING_SPREAD_WARN = 0.15   # P7: NBBO (ask-bid)/mid on any leg above this -> flag
LIQ_PENALTY = 0.5         # score deduction when flagged (per doctrine: never a block)
MIN_OPTION_OI = 100
MIN_OPTION_VOLUME = 10


def assess_liquidity(legs_raw: list[dict], rows: dict, *,
                     enforce_depth: bool = False) -> dict:
    """P7 — pure, TWS-free: given a card's raw legs and their quote rows
    ({(expiry,strike,cp): {bid,ask,mid,...} or None}), report which legs have
    no two-sided quote and which have a wide NBBO spread. This runs the SAME
    check whether repricing succeeded or fell back to model — previously a
    missing quote silently kept model prices with no visible flag at all.
    """
    no_quote, wide, low_oi, low_volume = [], [], [], []
    for leg in legs_raw:
        key = (leg["expiry"], leg["strike"], leg["cp"])
        r = rows.get(key)
        tag = f"{leg['strike']:g}{leg['cp']}"
        if not r or r.get("bid") is None or r.get("ask") is None or not r.get("mid"):
            no_quote.append(tag)
            continue
        spr = (r["ask"] - r["bid"]) / r["mid"] if r["mid"] else 1.0
        if spr > WING_SPREAD_WARN:
            wide.append({"leg": tag, "spread_pct": round(spr * 100, 1)})
        oi, volume = r.get("oi"), r.get("volume")
        if oi is None or float(oi) < MIN_OPTION_OI:
            low_oi.append({"leg": tag, "oi": oi})
        if volume is None or float(volume) < MIN_OPTION_VOLUME:
            low_volume.append({"leg": tag, "volume": volume})
    flagged = bool(no_quote or wide or (enforce_depth and (low_oi or low_volume)))
    return {"flagged": flagged, "no_quote_legs": no_quote, "wide_legs": wide,
            "low_oi_legs": low_oi, "low_volume_legs": low_volume,
            "minimum_oi": MIN_OPTION_OI, "minimum_volume": MIN_OPTION_VOLUME,
            "depth_enforced": enforce_depth}


def reprice_cards(ib, symbol: str, spot: float, today: date,
                  cards: list[dict], *, strict_option_liquidity: bool = False) -> None:
    from ib_insync import Option
    _st, _exch, tc, _idx = SURFACE_CFG.get(
        symbol, ("STK", "SMART", symbol, False))
    tc = next((leg.get("trading_class") for c in cards
               for leg in c.get("legs_raw", []) if leg.get("trading_class")), tc)
    keys = {(leg["expiry"], leg["strike"], leg["cp"])
            for c in cards for leg in c["legs_raw"]}
    opts = {k: Option(symbol, k[0].replace("-", ""), k[1], k[2], "SMART",
                      tradingClass=tc, currency="USD") for k in keys}
    ib.qualifyContracts(*opts.values())
    quotes = quote_many(ib, [o for o in opts.values() if o.conId],
                        fields="100,101" if strict_option_liquidity else "",
                        want_depth=strict_option_liquidity)
    rows = {k: quotes.get(o.conId) for k, o in opts.items() if o.conId}

    for c in cards:
        liq = assess_liquidity(c["legs_raw"], rows,
                               enforce_depth=strict_option_liquidity)
        c["liquidity"] = liq
        if liq["flagged"]:
            if liq["no_quote_legs"]:
                c["rationale"].append("LIQUIDITY: no live quote on "
                                      f"{', '.join(liq['no_quote_legs'])} — "
                                      "model price only, check illiquidity/off-hours")
            for w in liq["wide_legs"]:
                c["rationale"].append(f"LIQUIDITY: {w['leg']} NBBO spread "
                                      f"{w['spread_pct']:.0f}% — wide wing, check fill cost")
            if strict_option_liquidity and liq["low_oi_legs"]:
                c["rationale"].append(
                    f"LIQUIDITY: option OI below {MIN_OPTION_OI} or unavailable")
            if strict_option_liquidity and liq["low_volume_legs"]:
                c["rationale"].append(
                    f"LIQUIDITY: option volume below {MIN_OPTION_VOLUME} or unavailable")
            c["score"] = round(c["score"] - LIQ_PENALTY, 2)

        legs = [(leg, rows.get((leg["expiry"], leg["strike"], leg["cp"])))
                for leg in c["legs_raw"]]
        if any(r is None or r.get("mid") is None for _, r in legs):
            c["mid_src"] = "model"
            continue
        live_mid = round(sum(leg["qty"] * row["mid"] for leg, row in legs), 2)
        c["model_mid"] = c["net_mid"]
        c["net_mid"] = live_mid
        c["mid_src"] = "live"
        if all(r.get("greeks") for _, r in legs):
            # RiskNav units (x MULT) — matches strategies.base card greeks
            c["greeks"] = {k: round(sum(leg["qty"] * row["greeks"][k]
                                        for leg, row in legs) * MULT, 2)
                           for k in GREEK_KEYS}
        for raw, row in legs:
            if row and row.get("iv"):
                raw["iv"] = round(float(row["iv"]), 4)
        leg_objs = [Leg(cp=leg["cp"], strike=float(leg["strike"]),
                        expiry=date.fromisoformat(leg["expiry"]),
                        qty=int(leg["qty"]), iv=float(leg["iv"]))
                    for leg in c["legs_raw"]]
        m = struct_metrics(spot, leg_objs, today, entry=live_mid, q=q_for(symbol))
        c["max_profit"], c["max_loss"] = m["max_profit"], m["max_loss"]
        c["breakevens"] = m["breakevens"]
        c["rationale"].append(f"Live NBBO mid {live_mid:.2f} vs model "
                              f"{c['model_mid']:.2f} — card uses live")
