"""WatchlistSource interface (Requirement 1.1)."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import TickerEntry


class WatchlistUnavailable(Exception):
    """No watchlist could be resolved -- not even from cache.

    Distinguishable on purpose: the Agent logs an error and exits 0, because
    this is expected to self-resolve on the next scheduled run.
    """


class MissingCredentialError(Exception):
    """A required credential env var is absent -- the Agent exits 1."""


class WatchlistSource(ABC):
    @abstractmethod
    def fetch(self) -> list[TickerEntry]:
        """Resolve this run's watchlist."""
