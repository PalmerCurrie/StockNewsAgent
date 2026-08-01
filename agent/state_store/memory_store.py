"""In-process StateStore -- tests only.

``persistent = False``, which the Agent uses to refuse a `live` run: an
in-memory store would silently disable cross-run deduplication and spam alerts.
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from ..models import LockToken
from .base import StateStore


class MemoryStateStore(StateStore):
    persistent = False

    def __init__(self) -> None:
        self._kv: dict[str, tuple[bytes, Optional[float]]] = {}
        self._sets: dict[str, dict[str, Optional[float]]] = {}
        self._counters: dict[str, float] = {}
        self._locks: dict[str, tuple[str, float]] = {}

    def get(self, key: str) -> Optional[bytes]:
        entry = self._kv.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if _expired(expires_at):
            del self._kv[key]
            return None
        return value

    def set(self, key: str, value: bytes, ttl_seconds: Optional[int] = None) -> None:
        self._kv[key] = (value, time.time() + ttl_seconds if ttl_seconds else None)

    def delete(self, key: str) -> None:
        self._kv.pop(key, None)

    def add_to_set(self, key: str, member: str, ttl_seconds: int) -> None:
        self._sets.setdefault(key, {})[member] = (
            time.time() + ttl_seconds if ttl_seconds else None
        )

    def set_contains(self, key: str, member: str) -> bool:
        members = self._sets.get(key)
        if not members or member not in members:
            return False
        if _expired(members[member]):
            del members[member]
            return False
        return True

    def increment(self, key: str, amount: float) -> float:
        self._counters[key] = self._counters.get(key, 0.0) + amount
        return self._counters[key]

    def acquire_lock(self, key: str, ttl_seconds: int) -> Optional[LockToken]:
        held = self._locks.get(key)
        if held is not None and held[1] > time.time():
            return None
        token = str(uuid.uuid4())
        self._locks[key] = (token, time.time() + ttl_seconds)
        return LockToken(key=key, token=token, acquired_at=datetime.now(timezone.utc))

    def release_lock(self, token: LockToken) -> None:
        held = self._locks.get(token.key)
        if held is not None and held[0] == token.token:
            del self._locks[token.key]

    def read_lock_holder(self, key: str) -> Optional[str]:
        held = self._locks.get(key)
        if held is None or held[1] <= time.time():
            return None
        return held[0]

    def ping(self) -> None:
        return None


def _expired(expires_at: Optional[float]) -> bool:
    return expires_at is not None and expires_at <= time.time()
