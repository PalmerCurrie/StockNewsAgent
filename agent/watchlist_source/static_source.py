"""Static-file watchlist source (Requirement 1.2)."""

from __future__ import annotations

from ..models import AgentConfig, TickerEntry
from .base import WatchlistSource


class StaticWatchlistSource(WatchlistSource):
    """Returns ``config.watchlist`` as-is. Pure, no I/O.

    The config file having been loaded and validated already, a missing or
    unparseable file has been reported by the ConfigLoader before we get here.
    """

    def __init__(self, config: AgentConfig) -> None:
        self._config = config

    def fetch(self) -> list[TickerEntry]:
        return list(self._config.watchlist or [])
