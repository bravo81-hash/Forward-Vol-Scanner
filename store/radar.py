"""Durable daily/Friday watchlists for the Stock Opportunity Radar."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

from store.campaigns import DEFAULT_DB


class RadarStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.getenv("FVS_CAMPAIGN_DB") or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    def migrate(self):
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS radar_snapshots(
              id TEXT PRIMARY KEY, created_at REAL NOT NULL, session TEXT NOT NULL,
              cadence TEXT NOT NULL, source TEXT NOT NULL, policy_id TEXT NOT NULL,
              universe_size INTEGER NOT NULL, setup_count INTEGER NOT NULL,
              payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS radar_candidates(
              id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL, rank INTEGER NOT NULL,
              symbol TEXT NOT NULL, payload_json TEXT NOT NULL,
              FOREIGN KEY(snapshot_id) REFERENCES radar_snapshots(id));
            CREATE INDEX IF NOT EXISTS ix_radar_latest
              ON radar_snapshots(cadence, created_at DESC);
            CREATE INDEX IF NOT EXISTS ix_radar_symbol
              ON radar_candidates(symbol, snapshot_id);
            """)

    def save(self, payload: dict) -> dict:
        snapshot_id = "RAD-" + uuid4().hex[:12].upper()
        now = time.time()
        candidates = []
        with self.connect() as c:
            c.execute("""INSERT INTO radar_snapshots VALUES(?,?,?,?,?,?,?,?,?)""",
                      (snapshot_id, now, payload["session"], payload["cadence"],
                       payload["source"], payload.get("policy_id", "stock-radar-v1"),
                       int(payload.get("universe_size") or 0),
                       int(payload.get("setups_found") or 0),
                       json.dumps({k: v for k, v in payload.items() if k != "candidates"},
                                  separators=(",", ":"), default=str)))
            for idea in payload.get("candidates", []):
                cid = f"{snapshot_id}-{idea['symbol']}"
                saved = {**idea, "radar_candidate_id": cid, "snapshot_id": snapshot_id}
                c.execute("INSERT INTO radar_candidates VALUES(?,?,?,?,?)",
                          (cid, snapshot_id, int(idea["rank"]), idea["symbol"],
                           json.dumps(saved, separators=(",", ":"), default=str)))
                candidates.append(saved)
        return {**payload, "snapshot_id": snapshot_id, "created_at_epoch": now,
                "candidates": candidates}

    def latest(self, cadence: str = "daily") -> dict | None:
        with self.connect() as c:
            row = c.execute("""SELECT * FROM radar_snapshots WHERE cadence=?
                               ORDER BY created_at DESC LIMIT 1""", (cadence,)).fetchone()
            if not row:
                return None
            cards = c.execute("""SELECT payload_json FROM radar_candidates
                                 WHERE snapshot_id=? ORDER BY rank""", (row["id"],)).fetchall()
        meta = json.loads(row["payload_json"])
        meta.update(snapshot_id=row["id"], created_at_epoch=row["created_at"],
                    session=row["session"], cadence=row["cadence"], source=row["source"],
                    policy_id=row["policy_id"], universe_size=row["universe_size"],
                    setups_found=row["setup_count"],
                    candidates=[json.loads(x["payload_json"]) for x in cards])
        return meta

    def candidate(self, candidate_id: str) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT payload_json FROM radar_candidates WHERE id=?",
                            (candidate_id,)).fetchone()
        return json.loads(row["payload_json"]) if row else None

    def previous_symbols(self, cadence: str) -> set[str]:
        latest = self.latest(cadence)
        return {x["symbol"] for x in latest.get("candidates", [])} if latest else set()


_STORE: RadarStore | None = None


def radar_store() -> RadarStore:
    global _STORE
    if _STORE is None:
        _STORE = RadarStore()
    return _STORE
