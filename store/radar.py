"""Durable daily/Friday watchlists for the Stock Opportunity Radar."""
from __future__ import annotations

import json
import os
import sqlite3
import time
import hashlib
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
            CREATE TABLE IF NOT EXISTS radar_live_sessions(
              session TEXT NOT NULL, cadence TEXT NOT NULL, snapshot_id TEXT NOT NULL,
              prepared_at REAL NOT NULL, frozen_at REAL, payload_json TEXT NOT NULL,
              PRIMARY KEY(session, cadence));
            CREATE TABLE IF NOT EXISTS radar_entries(
              id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL UNIQUE,
              account TEXT NOT NULL, session TEXT NOT NULL, symbol TEXT NOT NULL,
              cluster_name TEXT NOT NULL, risk_amount REAL NOT NULL,
              quantity INTEGER NOT NULL, created_at REAL NOT NULL,
              payload_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_radar_entry_session
              ON radar_entries(account, session, created_at);
            CREATE TABLE IF NOT EXISTS radar_shadow_candidates(
              id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL,
              source_session TEXT NOT NULL, symbol TEXT NOT NULL, cohort TEXT NOT NULL,
              rank INTEGER NOT NULL, direction TEXT NOT NULL, trigger REAL NOT NULL,
              invalidation REAL NOT NULL, target REAL NOT NULL,
              triggered_session TEXT, entry_price REAL, payload_json TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS ix_radar_shadow_pending
              ON radar_shadow_candidates(triggered_session, source_session, symbol);
            CREATE TABLE IF NOT EXISTS radar_outcomes(
              shadow_id TEXT NOT NULL, horizon_days INTEGER NOT NULL,
              asof_session TEXT NOT NULL, return_pct REAL NOT NULL,
              mfe_pct REAL NOT NULL, mae_pct REAL NOT NULL,
              false_breakout INTEGER NOT NULL, invalidation_hit INTEGER NOT NULL,
              target_hit INTEGER NOT NULL, payload_json TEXT NOT NULL,
              PRIMARY KEY(shadow_id, horizon_days),
              FOREIGN KEY(shadow_id) REFERENCES radar_shadow_candidates(id));
            """)

    def save(self, payload: dict) -> dict:
        snapshot_id = "RAD-" + uuid4().hex[:12].upper()
        now = time.time()
        candidates = []
        with self.connect() as c:
            c.execute("""INSERT INTO radar_snapshots VALUES(?,?,?,?,?,?,?,?,?)""",
                      (snapshot_id, now, payload["session"], payload["cadence"],
                       payload["source"], payload.get("policy_id", "stock-radar-v2"),
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
            for idea in payload.get("research_pool", payload.get("candidates", [])):
                rank = int(idea.get("rank") or 999)
                cohort = ("v1_static" if rank <= 5 else
                          "v1_reserve" if rank <= 10 else "v1_research")
                material = f"{snapshot_id}:{cohort}:{idea['symbol']}"
                sid = "SHD-" + hashlib.sha256(material.encode()).hexdigest()[:20].upper()
                c.execute("""INSERT OR IGNORE INTO radar_shadow_candidates
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (sid, snapshot_id, payload["session"], idea["symbol"], cohort,
                           rank, idea["direction"], float(idea["trigger"]["price"]),
                           float(idea["invalidation"]), float(idea["target"]),
                           None, None,
                           json.dumps(idea, separators=(",", ":"), default=str)))
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

    def live_session(self, session: str, cadence: str) -> dict | None:
        with self.connect() as c:
            row = c.execute("""SELECT * FROM radar_live_sessions
                               WHERE session=? AND cadence=?""", (session, cadence)).fetchone()
        if not row:
            return None
        out = json.loads(row["payload_json"])
        out.update(session=row["session"], cadence=row["cadence"],
                   snapshot_id=row["snapshot_id"], prepared_at=row["prepared_at"],
                   frozen_at=row["frozen_at"])
        return out

    def save_live_session(self, session: str, cadence: str, snapshot_id: str,
                          payload: dict, *, frozen: bool = False) -> dict:
        now = time.time()
        with self.connect() as c:
            existing = c.execute("""SELECT prepared_at,frozen_at FROM radar_live_sessions
                                    WHERE session=? AND cadence=?""",
                                 (session, cadence)).fetchone()
            prepared = existing["prepared_at"] if existing else now
            frozen_at = (existing["frozen_at"] if existing and existing["frozen_at"]
                         else now if frozen else None)
            c.execute("""INSERT INTO radar_live_sessions VALUES(?,?,?,?,?,?)
                         ON CONFLICT(session,cadence) DO UPDATE SET
                         snapshot_id=excluded.snapshot_id,
                         frozen_at=COALESCE(radar_live_sessions.frozen_at, excluded.frozen_at),
                         payload_json=excluded.payload_json""",
                      (session, cadence, snapshot_id, prepared, frozen_at,
                       json.dumps(payload, separators=(",", ":"), default=str)))
        return self.live_session(session, cadence)

    def entry_usage(self, account: str, session: str) -> dict:
        with self.connect() as c:
            rows = c.execute("""SELECT * FROM radar_entries
                                WHERE account=? AND session=? ORDER BY created_at""",
                             (account, session)).fetchall()
        return {"count": len(rows),
                "risk_amount": round(sum(float(x["risk_amount"]) for x in rows), 2),
                "clusters": sorted({x["cluster_name"] for x in rows}),
                "symbols": [x["symbol"] for x in rows]}

    def record_entry(self, candidate_id: str, account: str, session: str,
                     symbol: str, cluster: str, risk_amount: float,
                     quantity: int, payload: dict) -> dict:
        entry_id = "ENT-" + hashlib.sha256(candidate_id.encode()).hexdigest()[:20].upper()
        with self.connect() as c:
            c.execute("""INSERT INTO radar_entries VALUES(?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(candidate_id) DO NOTHING""",
                      (entry_id, candidate_id, account, session, symbol, cluster,
                       float(risk_amount), int(quantity), time.time(),
                       json.dumps(payload, separators=(",", ":"), default=str)))
        return self.entry_usage(account, session)

    def shadow_symbols(self) -> list[str]:
        with self.connect() as c:
            return [x[0] for x in c.execute(
                "SELECT DISTINCT symbol FROM radar_shadow_candidates ORDER BY symbol").fetchall()]

    def record_shadow_candidates(self, snapshot_id: str, source_session: str,
                                 cohort: str, ideas: list[dict]) -> None:
        with self.connect() as c:
            for rank, idea in enumerate(ideas, 1):
                material = f"{snapshot_id}:{cohort}:{idea['symbol']}"
                sid = "SHD-" + hashlib.sha256(material.encode()).hexdigest()[:20].upper()
                c.execute("""INSERT OR IGNORE INTO radar_shadow_candidates
                             VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                          (sid, snapshot_id, source_session, idea["symbol"], cohort,
                           int(idea.get("rank") or rank), idea["direction"],
                           float(idea["trigger"]["price"]), float(idea["invalidation"]),
                           float(idea["target"]), None, None,
                           json.dumps(idea, separators=(",", ":"), default=str)))

    def update_outcomes(self, histories: dict[str, list[dict]]) -> dict:
        """Evaluate every saved static/reserve/challenger idea mechanically."""
        horizons = (1, 3, 5, 10, 20)
        updated = triggered = 0
        with self.connect() as c:
            rows = c.execute("SELECT * FROM radar_shadow_candidates").fetchall()
            for row in rows:
                bars = [x for x in histories.get(row["symbol"], [])
                        if str(x["date"]) > row["source_session"]]
                direction = 1 if row["direction"] == "BULL" else -1
                trigger_idx = next((i for i, bar in enumerate(bars)
                                    if (bar["high"] >= row["trigger"] if direction == 1
                                        else bar["low"] <= row["trigger"])), None)
                if trigger_idx is None:
                    continue
                triggered += 1
                path = bars[trigger_idx:]
                entry = float(row["trigger"])
                c.execute("""UPDATE radar_shadow_candidates
                             SET triggered_session=?,entry_price=? WHERE id=?""",
                          (path[0]["date"], entry, row["id"]))
                one_day_wrong = (len(path) > 1 and
                                 ((direction == 1 and path[1]["close"] < entry) or
                                  (direction == -1 and path[1]["close"] > entry)))
                for horizon in horizons:
                    if len(path) <= horizon:
                        continue
                    window = path[:horizon + 1]
                    target_hit = invalid_hit = invalid_before_target = False
                    for bar in window:
                        hit_invalid = (bar["low"] <= row["invalidation"] if direction == 1
                                       else bar["high"] >= row["invalidation"])
                        hit_target = (bar["high"] >= row["target"] if direction == 1
                                      else bar["low"] <= row["target"])
                        # Same-bar ordering is unknowable from daily OHLC, so
                        # treat it conservatively as invalidation first.
                        if hit_invalid and not target_hit:
                            invalid_before_target = True
                        invalid_hit = invalid_hit or hit_invalid
                        target_hit = target_hit or hit_target
                    ret = direction * (float(window[-1]["close"]) / entry - 1.0)
                    if direction == 1:
                        mfe = max(float(x["high"]) / entry - 1.0 for x in window)
                        mae = min(float(x["low"]) / entry - 1.0 for x in window)
                    else:
                        mfe = max(1.0 - float(x["low"]) / entry for x in window)
                        mae = min(1.0 - float(x["high"]) / entry for x in window)
                    false_breakout = bool(one_day_wrong or invalid_before_target)
                    detail = {"definition": "next close back through trigger, or invalidation before target",
                              "triggered_session": path[0]["date"]}
                    c.execute("""INSERT INTO radar_outcomes VALUES(?,?,?,?,?,?,?,?,?,?)
                                 ON CONFLICT(shadow_id,horizon_days) DO UPDATE SET
                                 asof_session=excluded.asof_session,
                                 return_pct=excluded.return_pct,mfe_pct=excluded.mfe_pct,
                                 mae_pct=excluded.mae_pct,false_breakout=excluded.false_breakout,
                                 invalidation_hit=excluded.invalidation_hit,
                                 target_hit=excluded.target_hit,payload_json=excluded.payload_json""",
                              (row["id"], horizon, window[-1]["date"], round(ret * 100, 3),
                               round(mfe * 100, 3), round(mae * 100, 3),
                               int(false_breakout), int(invalid_hit), int(target_hit),
                               json.dumps(detail, separators=(",", ":"))))
                    updated += 1
        return {"tracked": len(rows), "triggered": triggered, "outcomes_updated": updated}

    def evidence_summary(self) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("""SELECT s.cohort,o.horizon_days,COUNT(*) samples,
                                ROUND(AVG(o.return_pct),3) avg_return_pct,
                                ROUND(AVG(o.mfe_pct),3) avg_mfe_pct,
                                ROUND(AVG(o.mae_pct),3) avg_mae_pct,
                                ROUND(100.0*AVG(o.false_breakout),1) false_breakout_pct
                                FROM radar_outcomes o JOIN radar_shadow_candidates s
                                  ON s.id=o.shadow_id
                                GROUP BY s.cohort,o.horizon_days
                                ORDER BY s.cohort,o.horizon_days""").fetchall()
        return [dict(x) for x in rows]


_STORE: RadarStore | None = None


def radar_store() -> RadarStore:
    global _STORE
    if _STORE is None:
        _STORE = RadarStore()
    return _STORE
