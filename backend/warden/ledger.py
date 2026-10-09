"""Tamper-evident audit ledger.

`entries` holds the current state of each gated call (what the dashboard
shows). `chain` is append-only: every state change (decided, approved,
executed, settled...) is a record whose hash covers the previous record, so
editing or deleting history breaks verification.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from typing import Any

from .models import LedgerEntry

GENESIS = "0" * 64


def _h(prev: str, payload: dict[str, Any]) -> str:
    blob = prev + json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


class Ledger:
    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS entries (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE, at REAL, agent_id TEXT,
                session_id TEXT, tool TEXT, verdict TEXT, status TEXT, money REAL, counterparty TEXT, body TEXT);
            CREATE TABLE IF NOT EXISTS chain (seq INTEGER PRIMARY KEY AUTOINCREMENT, entry_id TEXT, at REAL, payload TEXT,
                prev_hash TEXT, hash TEXT);
            CREATE INDEX IF NOT EXISTS ix_entries_agent ON entries(agent_id, at);
            """
        )
        self.db.commit()

    # -- chain ----------------------------------------------------------
    def _tip(self) -> str:
        row = self.db.execute("SELECT hash FROM chain ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else GENESIS

    def _append_chain(self, entry_id: str, payload: dict[str, Any]) -> str:
        prev = self._tip()
        h = _h(prev, payload)
        self.db.execute(
            "INSERT INTO chain(entry_id, at, payload, prev_hash, hash) VALUES (?,?,?,?,?)",
            (entry_id, time.time(), json.dumps(payload, sort_keys=True, default=str), prev, h),
        )
        return h

    def verify(self) -> dict[str, Any]:
        prev = GENESIS
        n = 0
        for seq, payload, prev_hash, h in self.db.execute("SELECT seq, payload, prev_hash, hash FROM chain ORDER BY seq"):
            if prev_hash != prev or _h(prev, json.loads(payload)) != h:
                return {"ok": False, "broken_at": seq, "records": n}
            prev = h
            n += 1
        return {"ok": True, "records": n, "tip": prev}

    # -- entries --------------------------------------------------------
    def record(self, e: LedgerEntry) -> LedgerEntry:
        with self._lock:
            payload = {"event": "decided", "entry": e.model_dump(exclude={"seq", "hash", "prev_hash"}, mode="json")}
            e.prev_hash = self._tip()
            e.hash = self._append_chain(e.id, payload)
            cur = self.db.execute(
                "INSERT INTO entries(id, at, agent_id, session_id, tool, verdict, status, money, counterparty, body) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (e.id, e.at, e.agent_id, e.session_id, e.tool, e.verdict.value, e.status, e.money, e.counterparty, "{}"),
            )
            e.seq = cur.lastrowid or 0
            self.db.execute("UPDATE entries SET body=? WHERE id=?", (e.model_dump_json(), e.id))
            self.db.commit()
            return e

    def transition(self, entry_id: str, status: str, **fields: Any) -> LedgerEntry | None:
        with self._lock:
            row = self.db.execute("SELECT body FROM entries WHERE id=?", (entry_id,)).fetchone()
            if not row:
                return None
            e = LedgerEntry.model_validate_json(row[0])
            e.status = status
            for k, v in fields.items():
                setattr(e, k, v)
            payload = {"event": status, "entry_id": entry_id, **{k: v for k, v in fields.items() if k != "result"}}
            if "result" in fields:
                payload["result_digest"] = hashlib.sha256(json.dumps(fields["result"], sort_keys=True, default=str).encode()).hexdigest()
            e.hash = self._append_chain(entry_id, payload)
            self.db.execute("UPDATE entries SET status=?, body=? WHERE id=?", (status, e.model_dump_json(), entry_id))
            self.db.commit()
            return e

    def get(self, entry_id: str) -> LedgerEntry | None:
        row = self.db.execute("SELECT body FROM entries WHERE id=?", (entry_id,)).fetchone()
        return LedgerEntry.model_validate_json(row[0]) if row else None

    def all(self, limit: int = 500) -> list[LedgerEntry]:
        rows = self.db.execute("SELECT body FROM entries ORDER BY seq DESC LIMIT ?", (limit,)).fetchall()
        return [LedgerEntry.model_validate_json(r[0]) for r in rows]

    def recent(self, agent_id: str, since: float) -> list[LedgerEntry]:
        rows = self.db.execute(
            "SELECT body FROM entries WHERE agent_id=? AND at>=? ORDER BY seq", (agent_id, since)
        ).fetchall()
        return [LedgerEntry.model_validate_json(r[0]) for r in rows]

    def spent(self, agent_id: str, since: float) -> float:
        row = self.db.execute(
            "SELECT COALESCE(SUM(money),0) FROM entries WHERE agent_id=? AND at>=? AND status IN ('executed','settled') "
            "AND tool IN ('create_refund','create_payout','accept_dispute_claim')",
            (agent_id, since),
        ).fetchone()
        return float(row[0] or 0)

    def reset(self) -> None:
        with self._lock:
            self.db.executescript("DELETE FROM entries; DELETE FROM chain; DELETE FROM sqlite_sequence;")
            self.db.commit()
