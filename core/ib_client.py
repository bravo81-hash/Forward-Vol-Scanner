"""TWS connection, pacing-aware quoting, and session caching.

TWS limits respected here:
  * market-data lines: quotes go out in batches of MAX_LINES and are
    cancelled as soon as read — never more than MAX_LINES live at once
  * messages/sec: batch submits are spaced by PACE_S
  * historical data: one daily-bars request per symbol per session (cached)
  * chain params (reqSecDefOptParams): cached per symbol per session
"""
from __future__ import annotations
import asyncio
import math
import threading
import time

MAX_LINES = 40        # concurrent market-data lines per batch (default cap is ~100)
PACE_S = 0.05         # gap between request submits (50 msg/s API ceiling)
QUOTE_TIMEOUT = 8.0

DEFAULT_HOST = "127.0.0.1"    # TWS on this machine
DEFAULT_PORT = 7496           # 7497 = paper
_client_ids = iter(lambda: int(time.time() * 10) % 800 + 100, None)


class TTLCache:
    def __init__(self, ttl_s: float):
        self.ttl = ttl_s
        self._d: dict = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            v = self._d.get(key)
            if v and time.time() - v[0] < self.ttl:
                return v[1]
            self._d.pop(key, None)
            return None

    def put(self, key, val):
        with self._lock:
            self._d[key] = (time.time(), val)


CHAIN_CACHE = TTLCache(300)     # per-expiry surface: 5 min
BARS_CACHE = TTLCache(3600)     # daily bars: 1 h
PARAMS_CACHE = TTLCache(6 * 3600)


def with_ib(fn, host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Run fn(ib) on a fresh connection in its own thread + event loop."""
    out: dict = {}

    def target():
        asyncio.set_event_loop(asyncio.new_event_loop())
        from ib_insync import IB
        ib = IB()
        try:
            ib.connect(host, port, clientId=next(_client_ids), timeout=10)
            out["result"] = fn(ib)
        except Exception as e:        # noqa: BLE001 — surfaced to caller
            out["error"] = f"{type(e).__name__}: {e}"
        finally:
            if ib.isConnected():
                ib.disconnect()

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout=120)
    if "error" in out:
        raise RuntimeError(out["error"])
    return out.get("result")


def quote_many(ib, contracts, fields="", want_greeks=True, timeout=QUOTE_TIMEOUT):
    """Quote a list of qualified contracts in pacing-aware batches.

    Returns {conId: {bid, ask, mid, iv, oi, volume}}. Lines are cancelled
    per batch so concurrent usage never exceeds MAX_LINES.
    """
    res: dict = {}

    def val(v):
        return v if v is not None and not (isinstance(v, float) and math.isnan(v)) and v > 0 else None

    for i in range(0, len(contracts), MAX_LINES):
        batch = contracts[i:i + MAX_LINES]
        tickers = []
        for c in batch:
            tickers.append((c, ib.reqMktData(c, fields, snapshot=False)))
            ib.sleep(PACE_S)
        waited = 0.0
        while waited < timeout:
            ib.sleep(0.5)
            waited += 0.5
            ready = all(
                (val(t.bid) and val(t.ask)) and
                (not want_greeks or (t.modelGreeks and t.modelGreeks.impliedVol))
                for _, t in tickers)
            if ready:
                break
        for c, t in tickers:
            bid, ask = val(t.bid), val(t.ask)
            mid = round((bid + ask) / 2, 4) if bid and ask else None
            iv = t.modelGreeks.impliedVol if t.modelGreeks else None
            res[c.conId] = {"bid": bid, "ask": ask, "mid": mid,
                            "iv": iv if iv and 0.01 < iv < 3 else None,
                            "oi": getattr(t, "openInterest", None) or
                                  getattr(t, "putOpenInterest", None) or
                                  getattr(t, "callOpenInterest", None),
                            "volume": val(t.volume)}
            ib.cancelMktData(c)
    return res


def daily_bars(ib, contract, days=300):
    """Cached daily TRADES bars (one historical request/symbol/session)."""
    key = ("bars", contract.symbol, days)
    hit = BARS_CACHE.get(key)
    if hit is not None:
        return hit
    bars = ib.reqHistoricalData(contract, "", f"{days} D", "1 day",
                                "TRADES", useRTH=True, formatDate=1)
    out = [(b.date, b.open, b.high, b.low, b.close) for b in bars]
    BARS_CACHE.put(key, out)
    return out
