"""SQLite persistence. Single-user today, but every row carries user_id."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conv_user ON conversations(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,          -- JSON: string or list of API content blocks
    run_id TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id TEXT NOT NULL,
    user_text TEXT NOT NULL,
    status TEXT NOT NULL,           -- running | completed | error | cancelled | limit | refused | budget_exceeded
    model TEXT NOT NULL,
    steps INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    error TEXT,
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE INDEX IF NOT EXISTS idx_runs_user ON runs(user_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_conv ON runs(conversation_id, started_at);

CREATE TABLE IF NOT EXISTS run_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL,
    type TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON run_events(run_id, seq);

-- Money actually spent. Deliberately NOT linked to conversations/runs by a foreign key: deleting a chat
-- must not hand its spend back to the monthly budget.
CREATE TABLE IF NOT EXISTS usage_ledger (
    run_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    cost_usd REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_user ON usage_ledger(user_id, created_at);
"""


def new_id() -> str:
    return uuid.uuid4().hex[:16]


def _month_start_ts() -> float:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp()


class Database:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)
        # databases created before the ledger existed: carry their finished runs over (idempotent)
        self._exec(
            "INSERT OR IGNORE INTO usage_ledger (run_id, user_id, cost_usd, created_at) "
            "SELECT id, user_id, cost_usd, started_at FROM runs WHERE status != 'running'"
        )

    # -- low level -------------------------------------------------------
    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            return self._conn.execute(sql, params)

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- conversations ---------------------------------------------------
    def create_conversation(self, user_id: str, title: str = "New conversation") -> dict[str, Any]:
        cid, now = new_id(), time.time()
        self._exec(
            "INSERT INTO conversations (id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
            (cid, user_id, title, now, now),
        )
        return {"id": cid, "title": title, "created_at": now, "updated_at": now}

    def get_conversation(self, conversation_id: str, user_id: str) -> dict[str, Any] | None:
        return self._one(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE id=? AND user_id=?",
            (conversation_id, user_id),
        )

    def list_conversations(self, user_id: str, limit: int = 100) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id, title, created_at, updated_at FROM conversations WHERE user_id=? "
            "ORDER BY updated_at DESC LIMIT ?",
            (user_id, limit),
        )

    def rename_conversation(self, conversation_id: str, user_id: str, title: str) -> None:
        self._exec(
            "UPDATE conversations SET title=? WHERE id=? AND user_id=?", (title, conversation_id, user_id)
        )

    def delete_conversation(self, conversation_id: str, user_id: str) -> bool:
        cur = self._exec("DELETE FROM conversations WHERE id=? AND user_id=?", (conversation_id, user_id))
        return cur.rowcount > 0

    # -- messages (API-format history) -----------------------------------
    def add_messages(
        self, conversation_id: str, user_id: str, items: list[tuple[str, Any]], run_id: str | None
    ) -> None:
        """Insert several (role, content) rows atomically so history never ends on a dangling tool_use."""
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for role, content in items:
                    self._conn.execute(
                        "INSERT INTO messages (conversation_id, user_id, role, content, run_id, created_at) "
                        "VALUES (?,?,?,?,?,?)",
                        (conversation_id, user_id, role, json.dumps(content), run_id, now),
                    )
                self._conn.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?", (now, conversation_id)
                )
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def get_messages(self, conversation_id: str, user_id: str) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT role, content FROM messages WHERE conversation_id=? AND user_id=? ORDER BY id",
            (conversation_id, user_id),
        )
        return [{"role": r["role"], "content": json.loads(r["content"])} for r in rows]

    # -- runs ------------------------------------------------------------
    def create_run(
        self, run_id: str, conversation_id: str, user_id: str, user_text: str, model: str
    ) -> None:
        self._exec(
            "INSERT INTO runs (id, conversation_id, user_id, user_text, status, model, started_at) "
            "VALUES (?,?,?,?, 'running', ?, ?)",
            (run_id, conversation_id, user_id, user_text, model, time.time()),
        )

    def finish_run(self, run_id: str, status: str, stats: dict[str, Any], error: str | None) -> None:
        self._exec(
            "UPDATE runs SET status=?, steps=?, input_tokens=?, output_tokens=?, cache_read_tokens=?, "
            "cache_write_tokens=?, cost_usd=?, error=?, finished_at=? WHERE id=?",
            (
                status,
                stats.get("steps", 0),
                stats.get("input_tokens", 0),
                stats.get("output_tokens", 0),
                stats.get("cache_read_tokens", 0),
                stats.get("cache_write_tokens", 0),
                stats.get("cost_usd", 0.0),
                error,
                time.time(),
                run_id,
            ),
        )
        self._record_spend(run_id, float(stats.get("cost_usd", 0.0)))

    def _record_spend(self, run_id: str, cost_usd: float) -> None:
        self._exec(
            "INSERT OR REPLACE INTO usage_ledger (run_id, user_id, cost_usd, created_at) "
            "SELECT id, user_id, ?, started_at FROM runs WHERE id=?",
            (cost_usd, run_id),
        )

    def get_run(self, run_id: str, user_id: str) -> dict[str, Any] | None:
        return self._one("SELECT * FROM runs WHERE id=? AND user_id=?", (run_id, user_id))

    def list_runs(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._all(
            "SELECT * FROM runs WHERE user_id=? ORDER BY started_at DESC LIMIT ?", (user_id, limit)
        )

    def conversation_runs(self, conversation_id: str, user_id: str) -> list[dict[str, Any]]:
        runs = self._all(
            "SELECT * FROM runs WHERE conversation_id=? AND user_id=? ORDER BY started_at",
            (conversation_id, user_id),
        )
        for run in runs:
            run["events"] = self.get_events(run["id"])
        return runs

    def month_spend(self, user_id: str) -> float:
        row = self._one(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM usage_ledger WHERE user_id=? AND created_at>=?",
            (user_id, _month_start_ts()),
        )
        return float(row["total"]) if row else 0.0

    def fail_orphaned_runs(self) -> int:
        """Runs left 'running' by a crash/restart can never finish; close them out on startup.

        The money they already spent is recovered from their last persisted `usage` event.
        """
        orphans = self._all("SELECT id FROM runs WHERE status='running'")
        for run in orphans:
            last = self._one(
                "SELECT data FROM run_events WHERE run_id=? AND type='usage' ORDER BY seq DESC LIMIT 1",
                (run["id"],),
            )
            usage = json.loads(last["data"]) if last else {}
            self._exec(
                "UPDATE runs SET status='error', error='server restarted during run', steps=?, cost_usd=?, "
                "finished_at=? WHERE id=?",
                (usage.get("step", 0), usage.get("run_cost_usd", 0.0), time.time(), run["id"]),
            )
            self._record_spend(run["id"], float(usage.get("run_cost_usd", 0.0)))
        return len(orphans)

    # -- run events ------------------------------------------------------
    def add_event(self, run_id: str, seq: int, type_: str, data: dict[str, Any]) -> None:
        self._exec(
            "INSERT INTO run_events (run_id, seq, type, data, created_at) VALUES (?,?,?,?,?)",
            (run_id, seq, type_, json.dumps(data), time.time()),
        )

    def get_events(self, run_id: str, after: int = -1) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT seq, type, data FROM run_events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after)
        )
        return [{"seq": r["seq"], "type": r["type"], "data": json.loads(r["data"])} for r in rows]
