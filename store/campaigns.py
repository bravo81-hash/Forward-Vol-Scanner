"""Durable Campaign Engine v3 ledger and candidate store."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from uuid import uuid4

DEFAULT_DB = Path(__file__).with_name("campaigns.sqlite")
TERMINAL = {"CLOSED", "REJECTED"}
STATES = {"ELIGIBLE", "STAGED", "OPEN", "DEFENSIVE", "EXIT_PENDING",
          "CLOSED", "COOLDOWN", "REJECTED"}


def _dump(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class CampaignStore:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or os.getenv("FVS_CAMPAIGN_DB") or DEFAULT_DB)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.migrate()

    def connect(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        return c

    def migrate(self):
        with self.connect() as c:
            c.executescript("""
            CREATE TABLE IF NOT EXISTS candidates(
              id TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL,
              symbol TEXT NOT NULL, account TEXT, mode TEXT NOT NULL,
              policy_id TEXT NOT NULL, status TEXT NOT NULL,
              context_json TEXT NOT NULL, card_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS campaigns(
              id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              candidate_id TEXT NOT NULL, account TEXT, symbol TEXT NOT NULL,
              strategy TEXT NOT NULL, label TEXT NOT NULL, state TEXT NOT NULL,
              test_mode TEXT NOT NULL, quantity INTEGER NOT NULL,
              opened_at REAL, closed_at REAL, card_json TEXT NOT NULL,
              FOREIGN KEY(candidate_id) REFERENCES candidates(id));
            CREATE TABLE IF NOT EXISTS campaign_events(
              id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
              ts REAL NOT NULL, kind TEXT NOT NULL, from_state TEXT, to_state TEXT,
              payload_json TEXT NOT NULL,
              FOREIGN KEY(campaign_id) REFERENCES campaigns(id));
            CREATE TABLE IF NOT EXISTS manual_tests(
              id INTEGER PRIMARY KEY AUTOINCREMENT, campaign_id TEXT NOT NULL,
              ts REAL NOT NULL, source TEXT NOT NULL, setup_rating INTEGER,
              result_pct REAL, max_drawdown_pct REAL, notes TEXT,
              parameters_json TEXT NOT NULL,
              FOREIGN KEY(campaign_id) REFERENCES campaigns(id));
            CREATE TABLE IF NOT EXISTS snapshots(
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,
              symbol TEXT NOT NULL, account TEXT, source TEXT NOT NULL,
              fresh INTEGER NOT NULL, payload_json TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(
              id TEXT PRIMARY KEY, campaign_id TEXT, candidate_id TEXT NOT NULL,
              tws_order_id INTEGER, status TEXT NOT NULL, quantity INTEGER NOT NULL,
              limit_price REAL, transmit INTEGER NOT NULL DEFAULT 0,
              created_at REAL NOT NULL, updated_at REAL NOT NULL, payload_json TEXT NOT NULL,
              FOREIGN KEY(campaign_id) REFERENCES campaigns(id),
              FOREIGN KEY(candidate_id) REFERENCES candidates(id));
            CREATE TABLE IF NOT EXISTS fills(
              id TEXT PRIMARY KEY, order_id TEXT NOT NULL, ts REAL NOT NULL,
              quantity INTEGER NOT NULL, price REAL NOT NULL, commission REAL NOT NULL,
              payload_json TEXT NOT NULL, FOREIGN KEY(order_id) REFERENCES orders(id));
            CREATE INDEX IF NOT EXISTS ix_campaign_state ON campaigns(state, updated_at);
            CREATE INDEX IF NOT EXISTS ix_event_campaign ON campaign_events(campaign_id, ts);
            CREATE INDEX IF NOT EXISTS ix_order_campaign ON orders(campaign_id, updated_at);
            """)

    def save_candidate(self, symbol: str, account: str | None, mode: str,
                       policy_id: str, context: dict, card: dict,
                       ttl_seconds: int = 900) -> str:
        now = time.time()
        material = {"symbol": symbol, "account": account, "mode": mode,
                    "policy": policy_id, "session": context.get("session"),
                    "spot": context.get("spot"), "legs": card.get("legs_raw"),
                    "net_mid": card.get("net_mid"), "rule": card.get("evidence")}
        cid = hashlib.sha256(_dump(material).encode()).hexdigest()[:24]
        with self.connect() as c:
            c.execute("""INSERT INTO candidates VALUES(?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET
                         created_at=excluded.created_at, expires_at=excluded.expires_at,
                         status=excluded.status, context_json=excluded.context_json,
                         card_json=excluded.card_json""",
                      (cid, now, now + ttl_seconds, symbol, account, mode,
                       policy_id, "READY", _dump(context), _dump(card)))
        return cid

    def candidate(self, candidate_id: str, require_fresh: bool = False) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM candidates WHERE id=?", (candidate_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["context"], d["card"] = json.loads(d.pop("context_json")), json.loads(d.pop("card_json"))
        d["expired"] = d["expires_at"] < time.time()
        if require_fresh and d["expired"]:
            return None
        return d

    def create_campaign(self, candidate_id: str, quantity: int = 1,
                        test_mode: str = "optionnet") -> dict:
        cand = self.candidate(candidate_id)
        if not cand:
            raise KeyError("candidate not found")
        qty = max(int(quantity), 1)
        allowed = int(cand["card"].get("governor", {}).get("approved_lots") or 0)
        if allowed and qty > allowed:
            raise ValueError(f"quantity {qty} exceeds governor approval {allowed}")
        now, campaign_id = time.time(), "CMP-" + uuid4().hex[:12].upper()
        card = cand["card"]
        with self.connect() as c:
            c.execute("""INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (campaign_id, now, now, candidate_id, cand["account"], cand["symbol"],
                       card["strategy"], card["label"], "ELIGIBLE", test_mode, qty,
                       None, None, _dump(card)))
            c.execute("""INSERT INTO campaign_events
                         (campaign_id,ts,kind,from_state,to_state,payload_json)
                         VALUES(?,?,?,?,?,?)""",
                      (campaign_id, now, "created", None, "ELIGIBLE",
                       _dump({"candidate_id": candidate_id, "test_mode": test_mode})))
        return self.campaign(campaign_id)

    def transition(self, campaign_id: str, state: str, kind: str = "transition",
                   payload: dict | None = None) -> dict:
        if state not in STATES:
            raise ValueError(f"invalid campaign state {state}")
        current = self.campaign(campaign_id)
        if not current:
            raise KeyError("campaign not found")
        if current["state"] in TERMINAL and state != "COOLDOWN":
            raise ValueError(f"campaign is terminal ({current['state']})")
        now = time.time()
        opened = now if state == "OPEN" and not current.get("opened_at") else current.get("opened_at")
        closed = now if state == "CLOSED" else current.get("closed_at")
        with self.connect() as c:
            c.execute("UPDATE campaigns SET state=?,updated_at=?,opened_at=?,closed_at=? WHERE id=?",
                      (state, now, opened, closed, campaign_id))
            c.execute("""INSERT INTO campaign_events
                         (campaign_id,ts,kind,from_state,to_state,payload_json)
                         VALUES(?,?,?,?,?,?)""",
                      (campaign_id, now, kind, current["state"], state, _dump(payload or {})))
        return self.campaign(campaign_id)

    def add_event(self, campaign_id: str, kind: str, payload: dict) -> dict:
        current = self.campaign(campaign_id)
        if not current:
            raise KeyError("campaign not found")
        now = time.time()
        with self.connect() as c:
            c.execute("""INSERT INTO campaign_events
                         (campaign_id,ts,kind,from_state,to_state,payload_json)
                         VALUES(?,?,?,?,?,?)""",
                      (campaign_id, now, kind, current["state"], current["state"], _dump(payload)))
            c.execute("UPDATE campaigns SET updated_at=? WHERE id=?", (now, campaign_id))
        return self.campaign(campaign_id)

    def add_manual_test(self, campaign_id: str, payload: dict) -> dict:
        if not self.campaign(campaign_id):
            raise KeyError("campaign not found")
        rating = payload.get("setup_rating")
        if rating is not None and not 1 <= int(rating) <= 5:
            raise ValueError("setup_rating must be 1-5")
        with self.connect() as c:
            c.execute("""INSERT INTO manual_tests
                         (campaign_id,ts,source,setup_rating,result_pct,max_drawdown_pct,notes,parameters_json)
                         VALUES(?,?,?,?,?,?,?,?)""",
                      (campaign_id, time.time(), payload.get("source", "OptionNet Explorer"),
                       rating, payload.get("result_pct"), payload.get("max_drawdown_pct"),
                       payload.get("notes", ""), _dump(payload.get("parameters", {}))))
        return self.campaign(campaign_id)

    def save_snapshot(self, symbol: str, account: str | None, source: str,
                      fresh: bool, payload: dict) -> int:
        with self.connect() as c:
            cur = c.execute("INSERT INTO snapshots(ts,symbol,account,source,fresh,payload_json) VALUES(?,?,?,?,?,?)",
                            (time.time(), symbol, account, source, int(bool(fresh)), _dump(payload)))
            return int(cur.lastrowid)

    def campaign(self, campaign_id: str) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM campaigns WHERE id=?", (campaign_id,)).fetchone()
            if not row:
                return None
            events = c.execute("SELECT * FROM campaign_events WHERE campaign_id=? ORDER BY ts",
                               (campaign_id,)).fetchall()
            tests = c.execute("SELECT * FROM manual_tests WHERE campaign_id=? ORDER BY ts",
                              (campaign_id,)).fetchall()
        d = dict(row)
        d["card"] = json.loads(d.pop("card_json"))
        d["events"] = [{**dict(e), "payload": json.loads(e["payload_json"])} for e in events]
        for e in d["events"]:
            e.pop("payload_json", None)
        d["manual_tests"] = [{**dict(t), "parameters": json.loads(t["parameters_json"])} for t in tests]
        for t in d["manual_tests"]:
            t.pop("parameters_json", None)
        d["orders"] = self.campaign_orders(campaign_id)
        return d

    def campaigns(self, state: str | None = None, limit: int = 100) -> list[dict]:
        sql, args = "SELECT id FROM campaigns", []
        if state:
            sql += " WHERE state=?"
            args.append(state)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self.connect() as c:
            ids = [r[0] for r in c.execute(sql, args).fetchall()]
        return [self.campaign(i) for i in ids]

    def evidence_summary(self) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("""SELECT c.strategy, COUNT(t.id) tests,
                                AVG(t.setup_rating) avg_rating,
                                AVG(t.result_pct) avg_result_pct,
                                MIN(t.max_drawdown_pct) worst_drawdown_pct
                                FROM campaigns c LEFT JOIN manual_tests t ON t.campaign_id=c.id
                                GROUP BY c.strategy ORDER BY c.strategy""").fetchall()
        return [dict(r) for r in rows]

    def snapshots(self, symbol: str | None = None, limit: int = 500) -> list[dict]:
        sql, args = "SELECT * FROM snapshots", []
        if symbol:
            sql += " WHERE symbol=?"
            args.append(symbol)
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        with self.connect() as c:
            rows = c.execute(sql, args).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["payload"] = json.loads(d.pop("payload_json"))
            out.append(d)
        return out

    def record_order(self, candidate_id: str, quantity: int, result: dict,
                     campaign_id: str | None = None) -> dict:
        if not self.candidate(candidate_id):
            raise KeyError("candidate not found")
        if campaign_id and not self.campaign(campaign_id):
            raise KeyError("campaign not found")
        now = time.time()
        order_id = str(result.get("orderId") if result.get("orderId") not in (None, -1)
                       else "ORD-" + uuid4().hex[:12].upper())
        with self.connect() as c:
            c.execute("""INSERT INTO orders VALUES(?,?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                         updated_at=excluded.updated_at,payload_json=excluded.payload_json""",
                      (order_id, campaign_id, candidate_id, result.get("orderId"),
                       result.get("status", "Staged"), int(quantity), result.get("limit"),
                       0, now, now, _dump(result)))
        if campaign_id:
            self.add_event(campaign_id, "order_recorded", {"order_id": order_id,
                                                            "status": result.get("status")})
        return self.order(order_id)

    def record_fill(self, order_id: str, quantity: int, price: float,
                    commission: float = 0.0, payload: dict | None = None) -> dict:
        order = self.order(order_id)
        if not order:
            raise KeyError("order not found")
        fill_id = "FIL-" + uuid4().hex[:12].upper()
        with self.connect() as c:
            c.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?)",
                      (fill_id, order_id, time.time(), int(quantity), float(price),
                       float(commission), _dump(payload or {})))
            filled = c.execute("SELECT COALESCE(SUM(quantity),0) FROM fills WHERE order_id=?",
                               (order_id,)).fetchone()[0]
            status = "Filled" if filled >= order["quantity"] else "PartiallyFilled"
            c.execute("UPDATE orders SET status=?,updated_at=? WHERE id=?",
                      (status, time.time(), order_id))
        if order.get("campaign_id"):
            self.add_event(order["campaign_id"], "fill_recorded",
                           {"order_id": order_id, "fill_id": fill_id, "quantity": quantity,
                            "price": price, "commission": commission})
        return self.order(order_id)

    def order(self, order_id: str) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
            if not row:
                return None
            fills = c.execute("SELECT * FROM fills WHERE order_id=? ORDER BY ts", (order_id,)).fetchall()
        d = dict(row)
        d["payload"] = json.loads(d.pop("payload_json"))
        d["fills"] = []
        for fill in fills:
            f = dict(fill)
            f["payload"] = json.loads(f.pop("payload_json"))
            d["fills"].append(f)
        d["filled_quantity"] = sum(f["quantity"] for f in d["fills"])
        d["fees"] = round(sum(f["commission"] for f in d["fills"]), 2)
        d["average_fill"] = (round(sum(f["price"] * f["quantity"] for f in d["fills"])
                                   / d["filled_quantity"], 4)
                             if d["filled_quantity"] else None)
        return d

    def campaign_orders(self, campaign_id: str) -> list[dict]:
        with self.connect() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM orders WHERE campaign_id=? ORDER BY created_at",
                                           (campaign_id,)).fetchall()]
        return [self.order(i) for i in ids]


_STORE: CampaignStore | None = None


def campaign_store() -> CampaignStore:
    global _STORE
    if _STORE is None:
        _STORE = CampaignStore()
    return _STORE
