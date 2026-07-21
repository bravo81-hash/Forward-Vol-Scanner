"""Stage any N-leg Suggestion to TWS: whatIf margin first, then
transmit=False combo for manual review in TWS. Never auto-transmits.

TWS budget: N qualify calls + 1 whatIf + 1 placeOrder per staging.
"""
from __future__ import annotations
from core.chain import SURFACE_CFG


def _round_tick(x: float, tick: float = 0.05) -> float:
    return round(round(x / tick) * tick, 2)


def stage_suggestion(ib, symbol: str, sug_legs: list[dict], net_mid: float,
                     qty: int = 1, transmit: bool = False,
                     account: str | None = None) -> dict:
    if transmit:
        raise ValueError("automatic transmission is disabled; review and transmit manually in TWS")
    from ib_insync import ComboLeg, Contract, LimitOrder, Option
    _st, _exch, tc, _is_idx = SURFACE_CFG.get(
        symbol, ("STK", "SMART", symbol, False))
    tc = next((leg.get("trading_class") for leg in sug_legs
               if leg.get("trading_class")), tc)
    opts = []
    for leg in sug_legs:
        o = Option(symbol, leg["expiry"].replace("-", ""), leg["strike"], leg["cp"],
                   "SMART", tradingClass=tc, currency="USD")
        opts.append((leg, o))
    ib.qualifyContracts(*[o for _, o in opts])
    if any(not o.conId for _, o in opts):
        raise RuntimeError("leg qualification failed")

    combo = Contract(symbol=symbol, secType="BAG", currency="USD",
                     exchange="SMART")
    combo.comboLegs = [
        ComboLeg(conId=o.conId, ratio=abs(leg["qty"]),
                 action="BUY" if leg["qty"] > 0 else "SELL", exchange="SMART")
        for leg, o in opts]

    action = "BUY" if net_mid >= 0 else "SELL"
    px = _round_tick(abs(net_mid))
    order = LimitOrder(action, qty, px)
    if account:
        order.account = account
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
    if account:
        live.account = account
    live.transmit = False
    tr = ib.placeOrder(combo, live)
    ib.sleep(1.0)
    return {"orderId": tr.order.orderId, "action": action, "limit": px,
            "account": account,
            "qty": qty, "margin_change": margin, "transmit": live.transmit,
            "status": tr.orderStatus.status if tr.orderStatus else "Staged"}
