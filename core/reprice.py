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
from .pricing import struct_metrics

GREEK_KEYS = ("delta", "gamma", "theta", "vega")


def reprice_cards(ib, symbol: str, spot: float, today: date,
                  cards: list[dict]) -> None:
    from ib_insync import Option
    _st, _exch, tc, _idx = SURFACE_CFG[symbol]
    keys = {(l["expiry"], l["strike"], l["cp"])
            for c in cards for l in c["legs_raw"]}
    opts = {k: Option(symbol, k[0].replace("-", ""), k[1], k[2], "SMART",
                      tradingClass=tc, currency="USD") for k in keys}
    ib.qualifyContracts(*opts.values())
    quotes = quote_many(ib, [o for o in opts.values() if o.conId])
    rows = {k: quotes.get(o.conId) for k, o in opts.items() if o.conId}

    for c in cards:
        legs = [(l, rows.get((l["expiry"], l["strike"], l["cp"])))
                for l in c["legs_raw"]]
        if any(r is None or r.get("mid") is None for _, r in legs):
            c["mid_src"] = "model"
            continue
        live_mid = round(sum(l["qty"] * r["mid"] for l, r in legs), 2)
        c["model_mid"] = c["net_mid"]
        c["net_mid"] = live_mid
        c["mid_src"] = "live"
        if all(r.get("greeks") for _, r in legs):
            c["greeks"] = {k: round(sum(l["qty"] * r["greeks"][k]
                                        for l, r in legs), 4)
                           for k in GREEK_KEYS}
        leg_objs = [Leg(cp=l["cp"], strike=float(l["strike"]),
                        expiry=date.fromisoformat(l["expiry"]),
                        qty=int(l["qty"]), iv=float(l["iv"]))
                    for l in c["legs_raw"]]
        m = struct_metrics(spot, leg_objs, today, entry=live_mid)
        c["max_profit"], c["max_loss"] = m["max_profit"], m["max_loss"]
        c["breakevens"] = m["breakevens"]
        c["rationale"].append(f"Live NBBO mid {live_mid:.2f} vs model "
                              f"{c['model_mid']:.2f} — card uses live")
