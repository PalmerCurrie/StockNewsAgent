"""StateStore factory (Requirement 11.6, 11.7)."""

from __future__ import annotations

from ..models import StateStoreConfig
from .base import (
    KEY_ALREADY_ALERTED,
    KEY_DAILY_COST_LEDGER,
    KEY_RUN_LOCK,
    KEY_SUPPRESSED_ALERT,
    KEY_WATCHLIST_CACHE,
    RUN_LOCK_TTL_SECONDS,
    StateStore,
    StateStoreUnreachable,
)
from .memory_store import MemoryStateStore
from .sqlite_store import SQLiteStateStore

__all__ = [
    "StateStore",
    "StateStoreUnreachable",
    "MemoryStateStore",
    "SQLiteStateStore",
    "build_state_store",
    "KEY_ALREADY_ALERTED",
    "KEY_DAILY_COST_LEDGER",
    "KEY_RUN_LOCK",
    "KEY_SUPPRESSED_ALERT",
    "KEY_WATCHLIST_CACHE",
    "RUN_LOCK_TTL_SECONDS",
]


def build_state_store(config: StateStoreConfig) -> StateStore:
    """Instantiate the configured backend and verify it is reachable.

    Raises ``StateStoreUnreachable``; the Agent turns that into an exit-1.
    """
    backend = (config.type or "").lower()

    if backend == "redis":
        from .redis_store import RedisStateStore  # lazy: keeps redis optional for local dev

        store: StateStore = RedisStateStore()
    elif backend == "sqlite":
        if not config.path:
            raise StateStoreUnreachable("state_store.path is required for state_store.type: sqlite")
        store = SQLiteStateStore(config.path)
    elif backend == "memory":
        store = MemoryStateStore()
    else:
        raise StateStoreUnreachable(f"Unsupported state_store.type: {config.type!r}")

    store.ping()
    return store
