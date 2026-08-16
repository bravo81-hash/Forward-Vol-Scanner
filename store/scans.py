"""Durable store for pattern-scan results and background scan jobs.

These lived in two process-local dicts behind a lock in webapp.py, which had
two consequences: the app could not run more than one worker (a poll for a
job id would land on a worker that had never heard of it, and the TWS overlay
could not find the scan it was meant to overlay), and a restart mid-scan lost
the result with no way to recover it.

Same sqlite/WAL pattern as the campaign and radar stores, so the state model
of the app stays uniform.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

DEFAULT_DB = Path(__file__).with_name("scans.sqlite")

SCAN_TTL_S = float(os.getenv("FVS_PATTERN_SCAN_TTL_S", 30 * 60))
JOB_TTL_S = float(os.getenv("FVS_PATTERN_JOB_TTL_S", 60 * 60))


def _dump(value) -> str:
    return json.dumps(value, separators=(",", ":"), default=str)


class ScanStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.getenv("FVS_SCAN_DB") or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def migrate(self) -> None:
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS scans(
              id TEXT PRIMARY KEY, created_at REAL NOT NULL,
              payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS scan_jobs(
              id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              status TEXT NOT NULL, status_code INTEGER, error TEXT,
              result_json TEXT);
            CREATE INDEX IF NOT EXISTS ix_scan_created ON scans(created_at);
            CREATE INDEX IF NOT EXISTS ix_job_created ON scan_jobs(created_at);
            """)

    # ------------------------------------------------------------- scans --
    def save_scan(self, payload: dict) -> str:
        scan_id = uuid4().hex
        now = time.time()
        with self.connect() as c:
            c.execute("INSERT INTO scans(id, created_at, payload_json) VALUES(?,?,?)",
                      (scan_id, now, _dump(payload)))
            c.execute("DELETE FROM scans WHERE created_at < ?", (now - SCAN_TTL_S,))
        return scan_id

    def scan(self, scan_id: str) -> dict | None:
        cutoff = time.time() - SCAN_TTL_S
        with self.connect() as c:
            row = c.execute(
                "SELECT payload_json FROM scans WHERE id=? AND created_at >= ?",
                (scan_id, cutoff)).fetchone()
        return json.loads(row[0]) if row else None

    # -------------------------------------------------------------- jobs --
    def create_job(self) -> str:
        job_id = uuid4().hex
        now = time.time()
        with self.connect() as c:
            c.execute("INSERT INTO scan_jobs(id, created_at, updated_at, status) "
                      "VALUES(?,?,?,?)", (job_id, now, now, "queued"))
            c.execute("DELETE FROM scan_jobs WHERE created_at < ?", (now - JOB_TTL_S,))
        return job_id

    def update_job(self, job_id: str, *, status: str, result: dict | None = None,
                   error: str | None = None, status_code: int | None = None) -> None:
        with self.connect() as c:
            c.execute("""UPDATE scan_jobs
                         SET status=?, updated_at=?, result_json=COALESCE(?, result_json),
                             error=?, status_code=?
                         WHERE id=?""",
                      (status, time.time(), _dump(result) if result is not None else None,
                       error, status_code, job_id))

    def job(self, job_id: str) -> dict | None:
        cutoff = time.time() - JOB_TTL_S
        with self.connect() as c:
            row = c.execute(
                "SELECT * FROM scan_jobs WHERE id=? AND created_at >= ?",
                (job_id, cutoff)).fetchone()
        if not row:
            return None
        out = dict(row)
        raw = out.pop("result_json", None)
        out["result"] = json.loads(raw) if raw else None
        return out


_STORE: ScanStore | None = None


def scan_store() -> ScanStore:
    global _STORE
    path = os.getenv("FVS_SCAN_DB")
    if _STORE is None or (path and str(_STORE.path) != str(path)):
        _STORE = ScanStore(path)
    return _STORE
