"""Durable on-disk cache for free daily history.

`functools.lru_cache` on the yfinance loaders is per-process and dies with the
worker, so every restart — and every additional worker, scheduler thread or
CLI invocation — re-downloaded the same closed daily bars. Daily closes are
immutable once the session is over, so they are cached by (ticker, year) with
a short TTL on the current year only and no expiry on completed years.

Deliberately sqlite rather than parquet: the repo already depends on sqlite
for the campaign and radar stores, and this keeps a single durable-state
story with no new dependency.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import date
from pathlib import Path

DEFAULT_DB = Path(__file__).resolve().parents[1] / "store" / "barcache.sqlite"
#: Completed years never change; the live year is re-fetched this often.
CURRENT_YEAR_TTL_S = float(os.getenv("FVS_BARCACHE_TTL_S", 6 * 3600))

_lock = threading.Lock()


def _path() -> Path:
    return Path(os.getenv("FVS_BARCACHE_DB") or DEFAULT_DB)


def _connect() -> sqlite3.Connection:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path, timeout=10)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("""CREATE TABLE IF NOT EXISTS bars(
        ticker TEXT NOT NULL,
        year   INTEGER NOT NULL,
        fetched_at REAL NOT NULL,
        rows   TEXT NOT NULL,
        PRIMARY KEY (ticker, year))""")
    return con


def enabled() -> bool:
    return os.getenv("FVS_BARCACHE", "1") not in ("0", "false", "no")


def _fresh(year: int, fetched_at: float) -> bool:
    if year < date.today().year:
        return True
    return (time.time() - fetched_at) < CURRENT_YEAR_TTL_S


def get(ticker: str, year: int) -> tuple[tuple, ...] | None:
    if not enabled():
        return None
    try:
        with _lock, _connect() as con:
            row = con.execute(
                "SELECT fetched_at, rows FROM bars WHERE ticker=? AND year=?",
                (ticker, year)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not _fresh(year, float(row[0])):
        return None
    try:
        return tuple((date.fromisoformat(r[0]), *r[1:])
                     for r in json.loads(row[1]))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def put(ticker: str, year: int, rows: tuple[tuple, ...]) -> None:
    if not enabled() or not rows:
        return
    payload = json.dumps([[r[0].isoformat(), *[float(x) for x in r[1:]]]
                          for r in rows])
    try:
        with _lock, _connect() as con:
            con.execute(
                "INSERT INTO bars(ticker, year, fetched_at, rows) VALUES(?,?,?,?) "
                "ON CONFLICT(ticker, year) DO UPDATE SET fetched_at=excluded.fetched_at, "
                "rows=excluded.rows",
                (ticker, year, time.time(), payload))
    except sqlite3.Error:  # cache failures must never break a scan
        pass


def clear() -> None:
    try:
        with _lock, _connect() as con:
            con.execute("DELETE FROM bars")
    except sqlite3.Error:
        pass
