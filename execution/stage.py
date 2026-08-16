"""Stage any N-leg Suggestion to TWS: whatIf margin first, then
transmit=False combo for manual review in TWS. Never auto-transmits.

TWS budget: N qualify calls + 1 whatIf + 1 placeOrder per staging.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time

from config.loader import doctrine
from core.chain import SURFACE_CFG

#: Reject an identical staging request repeated inside this window. Staging
#: is a real TWS order (untransmitted, but present in the order book and
#: consuming a whatIf), so a double-clicked button used to produce two.
IDEMPOTENCY_WINDOW_S = float(doctrine("execution", "stage_idempotency_window_s", 20))
DEFAULT_TICK = float(doctrine("execution", "default_tick", 0.05))

_recent: dict[str, float] = {}
_recent_lock = threading.Lock()


class DuplicateStageError(RuntimeError):
    """Raised when the same staging request arrives twice in quick succession."""


def stage_fingerprint(symbol: str, sug_legs: list[dict], net_mid: float,
                      qty: int, account: str | None) -> str:
    material = {"symbol": symbol, "qty": int(qty), "account": account,
                "net_mid": round(float(net_mid), 2),
                "legs": sorted((leg.get("expiry"), leg.get("cp"),
                                float(leg.get("strike", 0)), int(leg.get("qty", 0)))
                               for leg in sug_legs)}
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()[:24]


def _claim(fingerprint: str, window_s: float) -> bool:
    """Return True if this request may proceed; False if it is a repeat."""
    if window_s <= 0:
        return True
    now = time.monotonic()
    with _recent_lock:
        for key in [k for k, v in _recent.items() if now - v > window_s]:
            _recent.pop(key, None)
        previous = _recent.get(fingerprint)
        if previous is not None and now - previous <= window_s:
            return False
        _recent[fingerprint] = now
        return True


def release_stage_claim(fingerprint: str) -> None:
    """Drop a claim so a genuinely retried staging is not blocked by a failure."""
    with _recent_lock:
        _recent.pop(fingerprint, None)


def combo_tick(opts) -> float:
    """Smallest tick common to every leg.

    A fixed 0.05 was wrong in both directions: SPX/SPXW penny-increment
    strikes and sub-$3 options tick at 0.01, so a rounded limit could be
    unmarketable or silently improved. Take the coarsest leg minTick that
    TWS actually reported, and fall back to the configured default only when
    no leg carries one.
    """
    ticks = []
    for contract in opts:
        tick = getattr(contract, "minTick", None)
        try:
            tick = float(tick)
        except (TypeError, ValueError):
            continue
        if tick > 0:
            ticks.append(tick)
    return max(ticks) if ticks else DEFAULT_TICK


def _round_tick(x: float, tick: float = DEFAULT_TICK) -> float:
    tick = tick if tick and tick > 0 else DEFAULT_TICK
    decimals = max(2, len(f"{tick:.10f}".rstrip("0").split(".")[1]))
    return round(round(x / tick) * tick, decimals)


def stage_suggestion(ib, symbol: str, sug_legs: list[dict], net_mid: float,
                     qty: int = 1, transmit: bool = False,
                     account: str | None = None,
                     idempotency_window_s: float | None = None) -> dict:
    if transmit:
        raise ValueError("automatic transmission is disabled; review and transmit manually in TWS")
    window = (IDEMPOTENCY_WINDOW_S if idempotency_window_s is None
              else float(idempotency_window_s))
    fingerprint = stage_fingerprint(symbol, sug_legs, net_mid, qty, account)
    if not _claim(fingerprint, window):
        raise DuplicateStageError(
            f"an identical staging request for {symbol} was submitted within the "
            f"last {window:.0f}s; check the TWS order book before retrying")
    try:
        return _stage(ib, symbol, sug_legs, net_mid, qty, account, fingerprint)
    except Exception:
        release_stage_claim(fingerprint)   # a failed attempt must stay retryable
        raise


def _stage(ib, symbol: str, sug_legs: list[dict], net_mid: float, qty: int,
           account: str | None, fingerprint: str) -> dict:
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
    tick = combo_tick([o for _, o in opts])
    px = _round_tick(abs(net_mid), tick)
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
            "account": account, "tick": tick, "fingerprint": fingerprint,
            "qty": qty, "margin_change": margin, "transmit": live.transmit,
            "status": tr.orderStatus.status if tr.orderStatus else "Staged"}
