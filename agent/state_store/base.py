"""StateStore interface (Requirement 11).

Six named records live here across runs:
  ``already_alerted``    set of Event identity keys, per-key TTL
  ``analyzed_stories``   set of Story keys already sent to the LLM, per-key TTL
  ``suppressed_alert``   Alert blob held over quiet hours
  ``run_lock``           overlap guard, 10-minute TTL
  ``daily_cost_ledger``  per-UTC-date LLM spend counter
  ``watchlist_cache``    last-known-good Notion watchlist, no TTL

``already_alerted`` and ``analyzed_stories`` are deliberately separate: the
first answers "have I already told the user about this?", the second answers
"have I already paid to analyze this?". Sharing one record ties spend
suppression to a successful dispatch, so a stretch of runs that find nothing
worth sending keeps re-buying the same analysis.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import LockToken

KEY_ALREADY_ALERTED = "already_alerted"
KEY_ANALYZED_STORIES = "analyzed_stories"
KEY_SUPPRESSED_ALERT = "suppressed_alert"
KEY_RUN_LOCK = "run_lock"
KEY_DAILY_COST_LEDGER = "daily_cost_ledger"  # suffixed with :<UTC date>
KEY_WATCHLIST_CACHE = "watchlist_cache"

RUN_LOCK_TTL_SECONDS = 600  # 10 minutes (Requirement 2.4)


class StateStoreUnreachable(Exception):
    """Backend could not be reached at startup -- the Agent exits 1."""


class StateStore(ABC):
    """Small KV + set + counter + lock store."""

    #: True for backends that survive process exit. `live` mode requires one.
    persistent: bool = True

    @abstractmethod
    def get(self, key: str) -> Optional[bytes]: ...

    @abstractmethod
    def set(self, key: str, value: bytes, ttl_seconds: Optional[int] = None) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def add_to_set(self, key: str, member: str, ttl_seconds: int) -> None: ...

    def add_to_set_many(self, key: str, members: list[str], ttl_seconds: int) -> None:
        """Add many members at once.

        The default walks ``add_to_set``; networked backends should override.
        A run analyses far more stories than it dispatches events, so this is
        the write-side counterpart to ``set_contains_many``.
        """
        for member in members:
            self.add_to_set(key, member, ttl_seconds)

    @abstractmethod
    def set_contains(self, key: str, member: str) -> bool: ...

    def set_contains_many(self, key: str, members: list[str]) -> set[str]:
        """Return the subset of ``members`` present in the set.

        The default walks ``set_contains``, which is fine for in-process
        backends. Networked backends should override with a batched form --
        one round trip per story is a minute of latency on a real watchlist.
        """
        return {member for member in members if self.set_contains(key, member)}

    @abstractmethod
    def increment(self, key: str, amount: float) -> float: ...

    @abstractmethod
    def acquire_lock(self, key: str, ttl_seconds: int) -> Optional[LockToken]:
        """Return a token on success, ``None`` if a live lock already exists."""

    @abstractmethod
    def release_lock(self, token: LockToken) -> None:
        """Delete the lock record iff ``token`` still owns it."""

    @abstractmethod
    def ping(self) -> None:
        """Raise ``StateStoreUnreachable`` if the backend is not usable."""

    def close(self) -> None:  # pragma: no cover - most backends need nothing
        return None
