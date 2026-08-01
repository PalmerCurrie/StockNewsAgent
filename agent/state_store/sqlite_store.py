"""SQLite-backed StateStore -- local development and testing only.

Not usable for the GitHub Actions deployment: runners have no persistent disk
between invocations, so anything written here disappears with the runner.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from ..models import LockToken
from .base import StateStore, StateStoreUnreachable

_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key        TEXT PRIMARY KEY,
    value      BLOB NOT NULL,
    expires_at REAL
);
CREATE TABLE IF NOT EXISTS set_members (
    key        TEXT NOT NULL,
    member     TEXT NOT NULL,
    expires_at REAL,
    PRIMARY KEY (key, member)
);
CREATE TABLE IF NOT EXISTS counters (
    key   TEXT PRIMARY KEY,
    value REAL NOT NULL DEFAULT 0
);
"""


class SQLiteStateStore(StateStore):
    persistent = True

    def __init__(self, path: str) -> None:
        self._path = path
        try:
            self._conn = sqlite3.connect(path, isolation_level=None, timeout=10)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.executescript(_SCHEMA)
        except sqlite3.Error as exc:
            raise StateStoreUnreachable(f"SQLite state store at {path} is unusable: {exc}") from exc

    # -- kv ---------------------------------------------------------------

    def get(self, key: str) -> Optional[bytes]:
        self._sweep()
        row = self._conn.execute(
            "SELECT value, expires_at FROM kv WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            return None
        value, expires_at = row
        if _expired(expires_at):
            self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            return None
        return bytes(value)

    def set(self, key: str, value: bytes, ttl_seconds: Optional[int] = None) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds else None
        self._conn.execute(
            "INSERT INTO kv (key, value, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
            "expires_at = excluded.expires_at",
            (key, value, expires_at),
        )

    def delete(self, key: str) -> None:
        self._conn.execute("DELETE FROM kv WHERE key = ?", (key,))

    # -- sets --------------------------------------------------------------

    def add_to_set(self, key: str, member: str, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds if ttl_seconds else None
        self._conn.execute(
            "INSERT INTO set_members (key, member, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key, member) DO UPDATE SET expires_at = excluded.expires_at",
            (key, member, expires_at),
        )

    def set_contains(self, key: str, member: str) -> bool:
        self._sweep()
        row = self._conn.execute(
            "SELECT expires_at FROM set_members WHERE key = ? AND member = ?", (key, member)
        ).fetchone()
        if row is None:
            return False
        if _expired(row[0]):
            self._conn.execute(
                "DELETE FROM set_members WHERE key = ? AND member = ?", (key, member)
            )
            return False
        return True

    # -- counters ----------------------------------------------------------

    def increment(self, key: str, amount: float) -> float:
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO counters (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
                (key, amount),
            )
            row = self._conn.execute(
                "SELECT value FROM counters WHERE key = ?", (key,)
            ).fetchone()
            self._conn.execute("COMMIT")
        except sqlite3.Error:
            self._conn.execute("ROLLBACK")
            raise
        return float(row[0]) if row else 0.0

    # -- locks -------------------------------------------------------------

    def acquire_lock(self, key: str, ttl_seconds: int) -> Optional[LockToken]:
        token = str(uuid.uuid4())
        lock_key = f"__lock__:{key}"
        now = time.time()
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._conn.execute(
                "SELECT value, expires_at FROM kv WHERE key = ?", (lock_key,)
            ).fetchone()
            if row is not None and not _expired(row[1]):
                self._conn.execute("COMMIT")
                return None
            self._conn.execute(
                "INSERT INTO kv (key, value, expires_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "expires_at = excluded.expires_at",
                (lock_key, token.encode(), now + ttl_seconds),
            )
            self._conn.execute("COMMIT")
        except sqlite3.Error:
            self._conn.execute("ROLLBACK")
            raise
        return LockToken(key=key, token=token, acquired_at=datetime.now(timezone.utc))

    def release_lock(self, token: LockToken) -> None:
        self._conn.execute(
            "DELETE FROM kv WHERE key = ? AND value = ?",
            (f"__lock__:{token.key}", token.token.encode()),
        )

    def read_lock_holder(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value, expires_at FROM kv WHERE key = ?", (f"__lock__:{key}",)
        ).fetchone()
        if row is None or _expired(row[1]):
            return None
        return bytes(row[0]).decode("utf-8", "replace")

    # -- lifecycle ---------------------------------------------------------

    def _sweep(self) -> None:
        """TTL semantics on read: drop anything already expired."""
        now = time.time()
        self._conn.execute("DELETE FROM kv WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,))
        self._conn.execute(
            "DELETE FROM set_members WHERE expires_at IS NOT NULL AND expires_at <= ?", (now,)
        )

    def ping(self) -> None:
        try:
            self._conn.execute("SELECT 1").fetchone()
        except sqlite3.Error as exc:
            raise StateStoreUnreachable(
                f"SQLite state store at {self._path} is unreachable: {exc}"
            ) from exc

    def close(self) -> None:
        try:
            self._conn.close()
        except sqlite3.Error:  # pragma: no cover - best effort
            pass


def _expired(expires_at: Optional[float]) -> bool:
    return expires_at is not None and expires_at <= time.time()
