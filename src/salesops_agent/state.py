"""Persistent state: conversation memory per user/session/thread, and a run history index.

One SQLite file (``state/salesops.db``) holds two things:

* **Conversation memory** — LangGraph's :class:`SqliteSaver` checkpointer stores
  each conversation's messages under a *thread key*. The key is derived from all
  three identifiers, ``user_id`` + ``session_id`` + ``thread_id``, so two users
  who happen to pick the same thread name get two separate conversations. That
  derivation is the isolation boundary: the agent can only ever load the
  checkpoint whose key it was given, and the key always includes the caller's
  user id.
* **Run history** — a ``runs`` table indexing every run (who, when, what was
  asked, the outcome, the version fingerprint, the trace file), which the
  ``history`` and ``replay`` commands read.

Identifiers are restricted to letters, digits, ``.``, ``_`` and ``-``. Without
that rule an id containing the key separator could be crafted to collide with
another user's key (``user="alice::s1"`` + ``session="x"``).
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langgraph.checkpoint.sqlite import SqliteSaver

ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
SEPARATOR = "::"
DB_NAME = "salesops.db"


class InvalidIdentifier(ValueError):
    """A user, session or thread id outside the allowed character set."""


def check_id(kind: str, value: str) -> str:
    """Validate one identifier and return it unchanged."""
    if not isinstance(value, str) or not ID_PATTERN.match(value):
        raise InvalidIdentifier(f"{kind} must be 1-64 characters of letters, digits, '.', '_' or '-'; got {value!r}")
    return value


@dataclass(frozen=True)
class Identity:
    """Who is asking, and in which conversation."""

    user_id: str
    session_id: str
    thread_id: str

    @classmethod
    def create(cls, user_id: str | None, session_id: str | None, thread_id: str | None, run_id: str) -> "Identity":
        """Fill defaults and validate.

        Defaults: user ``anonymous``; a fresh session per invocation; thread = the run id.
        With no ids given, every run is therefore its own conversation (no memory),
        which keeps one-off CLI questions and evaluation tasks independent.
        """
        return cls(
            user_id=check_id("user_id", user_id or "anonymous"),
            session_id=check_id("session_id", session_id or f"s-{uuid.uuid4().hex[:8]}"),
            thread_id=check_id("thread_id", thread_id or f"t-{run_id}"),
        )

    @property
    def thread_key(self) -> str:
        """The checkpointer key. Always includes the user id — the isolation boundary."""
        return SEPARATOR.join((self.user_id, self.session_id, self.thread_id))


class StateStore:
    """SQLite-backed checkpointer plus the run-history table."""

    def __init__(self, state_dir: Path):
        state_dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / DB_NAME
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.checkpointer = SqliteSaver(self.conn)
        self.checkpointer.setup()
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS runs (
                   run_id TEXT PRIMARY KEY,
                   timestamp TEXT NOT NULL,
                   user_id TEXT NOT NULL,
                   session_id TEXT NOT NULL,
                   thread_id TEXT NOT NULL,
                   task_id TEXT,
                   user_input TEXT NOT NULL,
                   final_status TEXT NOT NULL,
                   hitl_decision TEXT,
                   model TEXT,
                   versions TEXT NOT NULL,
                   trace_file TEXT NOT NULL
               )"""
        )
        self.conn.execute("CREATE INDEX IF NOT EXISTS runs_by_user ON runs (user_id, timestamp)")
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def record_run(self, entry: dict[str, Any]) -> None:
        """Index one finished run (from its already-redacted log entry)."""
        self.conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (entry["run_id"], entry["timestamp"], entry["user_id"], entry["session_id"], entry["thread_id"],
             entry.get("task_id"), entry["user_input"], entry["final_status"], entry.get("hitl_decision"),
             entry.get("model"), json.dumps(entry["versions"]), entry["trace_file"]),
        )
        self.conn.commit()

    def history(self, user_id: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """Most recent runs, optionally for one user only."""
        sql = "SELECT * FROM runs"
        params: tuple[Any, ...] = ()
        if user_id:
            sql += " WHERE user_id = ?"
            params = (check_id("user_id", user_id),)
        sql += " ORDER BY timestamp DESC, rowid DESC LIMIT ?"
        cur = self.conn.execute(sql, (*params, int(limit)))
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        cur = self.conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        row = cur.fetchone()
        return dict(zip([c[0] for c in cur.description], row)) if row else None

    def thread_messages(self, identity: Identity) -> list[Any]:
        """Messages stored for exactly this user/session/thread (empty if none)."""
        snapshot = self.checkpointer.get_tuple({"configurable": {"thread_id": identity.thread_key}})
        if snapshot is None:
            return []
        return list(snapshot.checkpoint.get("channel_values", {}).get("messages", []))
