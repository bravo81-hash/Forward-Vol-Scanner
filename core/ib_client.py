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
import itertools
import math
import os
import threading
import time

MAX_LINES = 40        # concurrent market-data lines per batch (default cap is ~100)
PACE_S = 0.05         # gap between request submits (50 msg/s API ceiling)
QUOTE_TIMEOUT = 8.0

DEFAULT_HOST = os.getenv("FVS_TWS_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("FVS_TWS_PORT", "7496"))  # 7497 = paper
CLIENT_ID_BASE = int(os.getenv("FVS_TWS_CLIENT_ID_BASE", "7100"))
CONNECT_TIMEOUT = float(os.getenv("FVS_TWS_CONNECT_TIMEOUT", "15"))
CONNECT_ATTEMPTS = max(1, int(os.getenv("FVS_TWS_CONNECT_ATTEMPTS", "2")))
_client_ids = itertools.count(CLIENT_ID_BASE)
_client_id_lock = threading.Lock()


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


TWS_JOB_TIMEOUT = 75
TWS_REQUEST_TIMEOUT = 15


def _next_client_id() -> int:
    """Return a process-unique ID so simultaneous web requests cannot collide."""
    with _client_id_lock:
        return next(_client_ids)


def _connection_error(host: str, port: int, client_id: int, exc: Exception) -> str:
    endpoint = f"{host}:{port} (client ID {client_id})"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return (
            f"TWS connection handshake timed out at {endpoint}. "
            "TWS accepted no complete API handshake. Confirm that TWS is fully logged in, "
            "Configure > API > Settings has 'Enable ActiveX and Socket Clients' enabled, "
            f"the Socket port is {port}, and 127.0.0.1 is trusted; then restart TWS and the app."
        )
    if isinstance(exc, (ConnectionRefusedError, OSError)):
        return (
            f"TWS is not accepting an API connection at {endpoint}: "
            f"{type(exc).__name__}: {exc}. Check that the app and TWS are on the same computer "
            f"and that the configured Socket port is {port}."
        )
    detail = str(exc).strip() or "no detail returned"
    return f"TWS connection failed at {endpoint}: {type(exc).__name__}: {detail}"


def _finish_result(out: dict, timed_out: bool, timeout_s: int = TWS_JOB_TIMEOUT):
    if timed_out:
        stage = out.get("stage", "request")
        raise RuntimeError(f"TWS {stage} timed out after {timeout_s} seconds; "
                           "check the IB data-farm status and retry")
    if "error" in out:
        raise RuntimeError(out["error"])
    if "result" not in out:
        raise RuntimeError("TWS request ended without a result; reconnect TWS and retry")
    return out["result"]


def with_ib(fn, host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Run fn(ib) on a fresh connection in its own thread + event loop."""
    out: dict = {}

    def target():
        asyncio.set_event_loop(asyncio.new_event_loop())
        from ib_insync import IB
        ib = None
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            out.pop("error", None)
            ib = IB()
            ib.RequestTimeout = TWS_REQUEST_TIMEOUT
            # Contract discovery is intentionally tolerant. IB returns error 200
            # for some unavailable expiry/strike combinations even when the rest
            # of the option chain is valid; ib_insync then leaves those contracts
            # unqualified so callers can filter conId == 0. Raising here would
            # abort the complete live scan on the first unusable combination.
            ib.RaiseRequestErrors = False
            client_id = _next_client_id()
            try:
                out["stage"] = "connection"
                ib.connect(host, port, clientId=client_id, timeout=CONNECT_TIMEOUT)
                out["connected"] = True
                out["stage"] = "market-data request"
                out["result"] = fn(ib)
                break
            except Exception as e:        # noqa: BLE001 — surfaced to caller
                if out.get("connected"):
                    detail = str(e).strip() or "no detail returned"
                    out["error"] = f"TWS request failed: {type(e).__name__}: {detail}"
                    break
                out["error"] = _connection_error(host, port, client_id, e)
                retryable = isinstance(e, (TimeoutError, asyncio.TimeoutError))
                if not retryable or attempt >= CONNECT_ATTEMPTS:
                    break
                time.sleep(0.75)
            finally:
                if ib.isConnected():
                    ib.disconnect()

    th = threading.Thread(target=target, daemon=True)
    th.start()
    th.join(timeout=TWS_JOB_TIMEOUT)
    return _finish_result(out, th.is_alive(), TWS_JOB_TIMEOUT)


def quote_many(ib, contracts, fields="", want_greeks=True, timeout=QUOTE_TIMEOUT,
               want_depth: bool = False):
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
            ready = all((val(t.bid) and val(t.ask)) and
                        (not want_greeks or (t.modelGreeks and t.modelGreeks.impliedVol)) and
                        (not want_depth or
                         ((val(getattr(t, "callOpenInterest", None)) or
                           val(getattr(t, "putOpenInterest", None))) and
                          (val(getattr(t, "callVolume", None)) or
                           val(getattr(t, "putVolume", None)))))
                        for _, t in tickers)
            if ready:
                break
        for c, t in tickers:
            bid, ask = val(t.bid), val(t.ask)
            mid = round((bid + ask) / 2, 4) if bid and ask else None
            mg = t.modelGreeks
            iv = mg.impliedVol if mg else None
            greeks = ({"delta": mg.delta, "gamma": mg.gamma,
                       "theta": mg.theta, "vega": mg.vega}
                      if mg and mg.delta is not None else None)
            last, close = val(getattr(t, "last", None)), val(getattr(t, "close", None))
            right = str(getattr(c, "right", "") or "").upper()
            oi = (getattr(t, "callOpenInterest", None) if right == "C" else
                  getattr(t, "putOpenInterest", None) if right == "P" else
                  getattr(t, "openInterest", None))
            volume = (getattr(t, "callVolume", None) if right == "C" else
                      getattr(t, "putVolume", None) if right == "P" else
                      getattr(t, "volume", None))
            res[c.conId] = {"bid": bid, "ask": ask, "mid": mid,
                            "last": last, "close": close, "greeks": greeks,
                            "iv": iv if iv and 0.01 < iv < 3 else None,
                            "oi": val(oi), "volume": val(volume)}
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
    if out:  # do not turn a temporary permission/feed failure into a 1 h outage
        BARS_CACHE.put(key, out)
    return out
