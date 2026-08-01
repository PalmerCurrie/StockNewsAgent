"""Redis-backed StateStore -- the default backend for the GitHub Actions deployment.

Everything maps onto native Redis commands, so all the atomicity guarantees
(lock acquisition, ledger increments) come from Redis itself.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from ..models import LockToken
from .base import StateStore, StateStoreUnreachable

_RELEASE_IF_OWNED = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""


class RedisStateStore(StateStore):
    persistent = True

    def __init__(self, url: Optional[str] = None, namespace: str = "stock_news_agent") -> None:
        import redis  # imported lazily so tests without redis installed still import the package

        self._url = url or os.environ.get("STATE_STORE_REDIS_URL", "")
        if not self._url:
            raise StateStoreUnreachable(
                "STATE_STORE_REDIS_URL is not set (required for state_store.type: redis)"
            )
        self._namespace = namespace
        try:
            self._client = redis.Redis.from_url(
                self._url, socket_timeout=10, socket_connect_timeout=10
            )
        except Exception as exc:  # noqa: BLE001 - surfaced as unreachable
            raise StateStoreUnreachable(f"Could not build a Redis client: {exc}") from exc
        self._release_script = self._client.register_script(_RELEASE_IF_OWNED)

    def _k(self, key: str) -> str:
        return f"{self._namespace}:{key}"

    # -- kv ---------------------------------------------------------------

    def get(self, key: str) -> Optional[bytes]:
        return self._client.get(self._k(key))

    def set(self, key: str, value: bytes, ttl_seconds: Optional[int] = None) -> None:
        if ttl_seconds:
            self._client.setex(self._k(key), ttl_seconds, value)
        else:
            self._client.set(self._k(key), value)

    def delete(self, key: str) -> None:
        self._client.delete(self._k(key))

    # -- sets --------------------------------------------------------------

    def add_to_set(self, key: str, member: str, ttl_seconds: int) -> None:
        """Per-member TTL via one key per member, plus a set for enumeration.

        Redis has no per-member set TTL, so membership is stored as an
        individual expiring key; the set itself is kept (with a refreshed TTL)
        only for debugging/inspection.
        """
        pipe = self._client.pipeline()
        pipe.setex(self._k(f"{key}:{member}"), ttl_seconds, b"1")
        pipe.sadd(self._k(key), member)
        pipe.expire(self._k(key), ttl_seconds)
        pipe.execute()

    def add_to_set_many(self, key: str, members: list[str], ttl_seconds: int) -> None:
        """One round trip instead of one per member."""
        unique = list(dict.fromkeys(members))
        if not unique:
            return
        pipe = self._client.pipeline()
        for member in unique:
            pipe.setex(self._k(f"{key}:{member}"), ttl_seconds, b"1")
        pipe.sadd(self._k(key), *unique)
        pipe.expire(self._k(key), ttl_seconds)
        pipe.execute()

    def set_contains(self, key: str, member: str) -> bool:
        return bool(self._client.exists(self._k(f"{key}:{member}")))

    def set_contains_many(self, key: str, members: list[str]) -> set[str]:
        """One round trip instead of one per member."""
        if not members:
            return set()
        unique = list(dict.fromkeys(members))
        pipe = self._client.pipeline()
        for member in unique:
            pipe.exists(self._k(f"{key}:{member}"))
        return {member for member, present in zip(unique, pipe.execute()) if present}

    # -- counters ----------------------------------------------------------

    def increment(self, key: str, amount: float) -> float:
        return float(self._client.incrbyfloat(self._k(key), amount))

    # -- locks -------------------------------------------------------------

    def acquire_lock(self, key: str, ttl_seconds: int) -> Optional[LockToken]:
        token = str(uuid.uuid4())
        acquired = self._client.set(self._k(key), token, nx=True, ex=ttl_seconds)
        if not acquired:
            return None
        return LockToken(key=key, token=token, acquired_at=datetime.now(timezone.utc))

    def release_lock(self, token: LockToken) -> None:
        self._release_script(keys=[self._k(token.key)], args=[token.token])

    def read_lock_holder(self, key: str) -> Optional[str]:
        value = self._client.get(self._k(key))
        return value.decode("utf-8", "replace") if value else None

    # -- lifecycle ---------------------------------------------------------

    def ping(self) -> None:
        try:
            self._client.ping()
        except Exception as exc:  # noqa: BLE001 - any redis failure is "unreachable"
            raise StateStoreUnreachable(
                f"Redis at {_redact(self._url)} is unreachable: {exc}{_tls_hint(self._url)}"
            )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001 - best effort
            pass


def _redact(url: str) -> str:
    """Strip credentials so the URL can appear in a log line."""
    if "@" in url:
        scheme, _, rest = url.partition("://")
        return f"{scheme}://***@{rest.split('@', 1)[-1]}"
    return url


def _tls_hint(url: str) -> str:
    """Managed Redis almost always requires TLS; a plain redis:// URL against
    one fails as a bare connection drop, which is not self-explanatory."""
    if url.startswith("redis://"):
        return (
            " -- the connection string starts with 'redis://', but managed Redis "
            "(Upstash, Redis Cloud, ...) requires TLS. Try 'rediss://' (two s's)."
        )
    return ""
