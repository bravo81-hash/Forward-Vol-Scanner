"""SQLite audit log: every shortlist served + every order staged.
Feeds the future edge-audit (suggested vs chosen vs outcome)."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path

DB = Path(__file__).with_name("audit.sqlite")


def _conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS events(
        ts REAL, kind TEXT, symbol TEXT, payload TEXT)""")
    return c


def log(kind: str, symbol: str, payload: dict):
    with _conn() as c:
        c.execute("INSERT INTO events VALUES (?,?,?,?)",
                  (time.time(), kind, symbol, json.dumps(payload, default=str)))


def recent(n=50):
    with _conn() as c:
        return c.execute(
            "SELECT ts,kind,symbol,payload FROM events ORDER BY ts DESC LIMIT ?",
            (n,)).fetchall()
