"""Stage any N-leg Suggestion to TWS: whatIf margin first, then
transmit=False combo for manual review in TWS. Never auto-transmits.

TWS budget: N qualify calls + 1 whatIf + 1 placeOrder per staging.
"""
from __future__ import annotations
from core.chain import SURFACE_CFG
from core.models import Suggestion


def _round_tick(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, 2)


def stage_suggestion(ib, symbol: str, sug_legs: list[dict], net_mid: float,
                     qty: int = 1, transmit: bool = False) -> dict:
    from ib_insync import ComboLeg, Contract, LimitOrder, Option
    st, exch, tc, is_idx = SURFACE_CFG[symbol]
    opts = []
    for l in sug_legs:
        o = Option(symbol, l["expiry"].replace("-", ""), l["strike"], l["cp"],
                   "SMART", tradingClass=tc, currency="USD")
        opts.append((l, o))
    ib.qualifyContracts(*[o for _, o in opts])
    if any(not o.conId for _, o in opts):
        raise RuntimeError("leg qualification failed")

    combo = Contract(symbol=symbol, secType="BAG", currency="USD",
                     exchange="SMART")
    combo.comboLegs = [
        ComboLeg(conId=o.conId, ratio=abs(l["qty"]),
                 action="BUY" if l["qty"] > 0 else "SELL", exchange="SMART")
        for l, o in opts]

    action = "BUY" if net_mid >= 0 else "SELL"
    px = _round_tick(abs(net_mid))
    order = LimitOrder(action, qty, px)
    order.transmit = False
    order.whatIf = True
    wi = ib.placeOrder(combo, order)
    ib.sleep(2.0)
    margin = None
    if wi.orderStatus and getattr(wi.orderStatus, "initMarginChange", None):
        try:
            margin = float(wi.orderStatus.initMarginChange)
        except (TypeError, ValueError):
            margin = None
    ib.cancelOrder(order)

    live = LimitOrder(action, qty, px)
    live.transmit = bool(transmit)
    tr = ib.placeOrder(combo, live)
    ib.sleep(1.0)
    return {"orderId": tr.order.orderId, "action": action, "limit": px,
            "qty": qty, "margin_change": margin, "transmit": live.transmit,
            "status": tr.orderStatus.status if tr.orderStatus else "Staged"}
