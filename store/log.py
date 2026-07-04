"""SQLite audit log: every shortlist served + every order staged.
Feeds the future edge-audit (suggested vs chosen vs outcome)."""
from __future__ import annotations
import json
import sqlite3
import time
from datetime import date
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


def _scan_conn():
    c = sqlite3.connect(DB)
    c.execute("""CREATE TABLE IF NOT EXISTS scans(
        ts REAL, day TEXT, symbol TEXT, account TEXT, mode TEXT,
        spot REAL, trend TEXT, vol_state TEXT, vrp REAL, verdict TEXT,
        size TEXT, cell TEXT, cards TEXT, taken INTEGER)""")
    return c


def log_scan(sl: dict, account: str | None, mode: str) -> None:
    """P3: one structured, queryable row per scan (regime cell + size + cards).
    Never raise into the request path — journaling is best-effort."""
    try:
        r = sl.get("regime", {})
        cell = f"{r.get('vol_state','?')}·{r.get('trend','?')}"
        with _scan_conn() as c:
            c.execute("INSERT INTO scans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (time.time(), date.today().isoformat(),
                       sl.get("symbol"), account, mode, sl.get("spot"),
                       r.get("trend"), r.get("vol_state"), r.get("vrp"),
                       sl.get("verdict"), sl.get("size"), cell,
                       json.dumps([c.get("label") for c in sl.get("cards", [])]),
                       0))
    except Exception:                                # noqa: BLE001 — never fatal
        pass


def scans_by_cell(n=500):
    """Hit-rate scaffolding: verdict/size distribution per regime cell."""
    with _scan_conn() as c:
        return c.execute(
            "SELECT cell, size, COUNT(*) FROM scans GROUP BY cell, size "
            "ORDER BY COUNT(*) DESC LIMIT ?", (n,)).fetchall()
