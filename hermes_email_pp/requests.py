"""Durable request snapshots and lifecycle state, without host dependencies."""

from __future__ import annotations

import json
import math
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

_TERMINAL_STATES = frozenset(
    {
        "sent",
        "completed",
        "smtp_accepted",
        "failed",
        "cancelled",
        "expired",
        "delivery_uncertain",
    }
)


class RequestStore:
    """Durable JSON snapshots, with SQLite-serialized writes across threads.

    Supply a dedicated database path: its immediate directory is made 0700,
    newly created ancestors are 0700, and the database is made 0600. Connections
    are operation-scoped; instances can be shared by threads or reopened safely.
    Call recover() at startup, before admitting workers, not while work is live.
    Terminal states are sent/completed/smtp_accepted, failed, cancelled, and
    delivery_uncertain. The low-level transition() is a CAS, not a policy engine.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).absolute()
        if self.path.is_symlink():
            raise ValueError("database path must not be a symbolic link")
        missing = []
        parent = self.path.parent
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        for directory in reversed(missing):
            directory.mkdir(mode=0o700, exist_ok=True)
        self.path.parent.chmod(0o700)
        descriptor = os.open(
            self.path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)
        with self._connection(write=True) as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY,
                    message_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )"""
            )
            connection.execute(
                """CREATE TABLE IF NOT EXISTS notifications (
                    id TEXT PRIMARY KEY,
                    request_id TEXT NOT NULL REFERENCES requests(id),
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    sent_at REAL,
                    UNIQUE (request_id, kind, text)
                )"""
            )

        with self._connection(write=True) as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS owner_question_refs ("
                "ref INTEGER PRIMARY KEY AUTOINCREMENT, "
                "request_id TEXT NOT NULL REFERENCES requests(id), "
                "question_id TEXT NOT NULL UNIQUE)"
            )

    def question_ref(self, id: str, question_id: str) -> int:
        """Allocate a durable, never-reused human reference to one question."""
        with self._connection(write=True) as connection:
            request = self._request(self._require(connection, id))
            if request.get("question_id") != question_id:
                raise ValueError("question does not match this request")
            connection.execute(
                "INSERT INTO owner_question_refs(request_id, question_id) "
                "VALUES (?, ?) "
                "ON CONFLICT(question_id) DO NOTHING",
                (id, question_id),
            )
            return connection.execute(
                "SELECT ref FROM owner_question_refs WHERE question_id = ?",
                (question_id,),
            ).fetchone()[0]

    def resolve_question_ref(self, ref: int) -> tuple[str, str]:
        if not 0 < ref < (1 << 63):
            raise ValueError("invalid question reference")
        with self._connection() as connection:
            row = connection.execute(
                "SELECT request_id, question_id FROM owner_question_refs WHERE ref = ?",
                (ref,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown question reference")
            return row[0], row[1]

    @contextmanager
    def _connection(self, *, write: bool = False):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _request(row: sqlite3.Row) -> dict[str, Any]:
        request = json.loads(row["payload"])
        request.update({key: row[key] for key in row.keys() if key != "payload"})
        return request

    def create(self, message_key: str, payload: dict) -> tuple[dict[str, Any], bool]:
        """Return the existing request for a repeated trigger, without mutation."""
        now = time.time()
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT * FROM requests WHERE message_key = ?", (message_key,)
            ).fetchone()
            if existing is not None:
                return self._request(existing), False
            id = secrets.token_urlsafe(24)
            connection.execute(
                "INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)",
                (id, message_key, "accepted", json.dumps(payload), now, now),
            )
            row = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (id,)
            ).fetchone()
            return self._request(row), True

    def get(self, id: str) -> dict[str, Any] | None:
        """Return a detached snapshot, or None for an unknown identity."""
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM requests WHERE id = ?", (id,)
            ).fetchone()
            return self._request(row) if row is not None else None

    def list_active(self) -> list[dict[str, Any]]:
        """List unfinished requests in creation order, never retrying uncertainty."""
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM requests ORDER BY created_at, id"
            ).fetchall()
            return [
                self._request(row)
                for row in rows
                if row["state"] not in _TERMINAL_STATES
            ]

    def transition(
        self, id: str, expected: str | list[str], state: str, **fields: Any
    ) -> dict[str, Any]:
        """Atomically compare state and persist the next state plus result fields."""
        expected_states = [expected] if isinstance(expected, str) else expected
        with self._connection(write=True) as connection:
            row = self._require(connection, id)
            if row["state"] not in expected_states:
                raise ValueError("request state does not match expected state")
            return self._update(connection, row, state, fields)

    def ask(self, id: str, question: str, ttl_seconds: float = 86400) -> dict[str, Any]:
        """Persist a fresh question token and hold the request for its owner."""
        if (
            isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be a positive finite number")
        with self._connection(write=True) as connection:
            row = self._require(connection, id)
            if row["state"] in _TERMINAL_STATES or row["state"] == "sending":
                raise ValueError("request state cannot wait for an owner answer")
            return self._update(
                connection,
                row,
                "waiting_for_owner",
                {
                    "question": question,
                    "question_id": secrets.token_urlsafe(24),
                    "expires_at": time.time() + ttl_seconds,
                },
            )

    def answer(self, id: str, question_id: str, answer: Any) -> dict[str, Any]:
        """Consume exactly one live, request-bound owner answer atomically."""
        with self._connection(write=True) as connection:
            row = self._require(connection, id)
            payload = json.loads(row["payload"])
            if row["state"] != "waiting_for_owner":
                raise ValueError("request is not waiting for an owner answer")
            if payload.get("question_id") != question_id:
                raise ValueError("question does not match this request")
            if time.time() >= payload["expires_at"]:
                raise ValueError("question has expired")
            return self._update(connection, row, "ready", {"answer": answer})

    def cancel(self, id: str) -> dict[str, Any]:
        """Cancel unfinished work, never an in-flight or uncertain SMTP send."""
        with self._connection(write=True) as connection:
            row = self._require(connection, id)
            if row["state"] in _TERMINAL_STATES or row["state"] == "sending":
                raise ValueError("request state cannot be cancelled")
            return self._update(connection, row, "cancelled", {})

    def recover(self) -> None:
        """At startup, retry interrupted work but never blindly retry SMTP."""
        with self._connection(write=True) as connection:
            now = time.time()
            connection.execute(
                "UPDATE requests SET state = CASE state "
                "WHEN 'working' THEN 'ready' ELSE 'delivery_uncertain' END, "
                "updated_at = ? WHERE state IN ('working', 'sending')",
                (now,),
            )

    def notification(self, id: str, kind: str, text: str) -> dict[str, Any]:
        """Enqueue one durable notice per (request, kind, text), without sending."""
        with self._connection(write=True) as connection:
            self._require(connection, id)
            connection.execute(
                "INSERT INTO notifications "
                "(id, request_id, kind, text, created_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(request_id, kind, text) DO NOTHING",
                (secrets.token_urlsafe(24), id, kind, text, time.time()),
            )
            row = connection.execute(
                "SELECT * FROM notifications WHERE request_id = ? AND kind = ? "
                "AND text = ?",
                (id, kind, text),
            ).fetchone()
            return dict(row)

    def pending_notifications(self) -> list[dict[str, Any]]:
        """Return detached pending notices in creation order."""
        with self._connection() as connection:
            return [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM notifications WHERE sent_at IS NULL "
                    "ORDER BY created_at, id"
                )
            ]

    def notification_sent(self, notification_id: str) -> None:
        """Acknowledge a notice without changing an earlier acknowledgment."""
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                "UPDATE notifications SET sent_at = COALESCE(sent_at, ?) WHERE id = ?",
                (time.time(), notification_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("unknown notification")

    @staticmethod
    def _require(connection: sqlite3.Connection, id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM requests WHERE id = ?", (id,)
        ).fetchone()
        if row is None:
            raise ValueError("unknown request")
        return row

    def _update(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        state: str,
        fields: dict[str, Any],
    ) -> dict[str, Any]:
        payload = json.loads(row["payload"])
        if state == "waiting_for_owner" and "question_id" in fields:
            payload.pop("answer", None)
        payload.update(fields)
        connection.execute(
            "UPDATE requests SET state = ?, payload = ?, updated_at = ? WHERE id = ?",
            (state, json.dumps(payload), time.time(), row["id"]),
        )
        return self._request(self._require(connection, row["id"]))
