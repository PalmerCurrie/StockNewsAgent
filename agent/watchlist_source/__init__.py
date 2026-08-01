"""WatchlistSource factory (Requirement 1.1)."""

from __future__ import annotations

from ..logger import Logger
from ..models import AgentConfig
from ..state_store.base import StateStore
from .base import MissingCredentialError, WatchlistSource, WatchlistUnavailable
from .notion_source import NotionWatchlistSource
from .static_source import StaticWatchlistSource

__all__ = [
    "WatchlistSource",
    "WatchlistUnavailable",
    "MissingCredentialError",
    "StaticWatchlistSource",
    "NotionWatchlistSource",
    "build_watchlist_source",
]


def build_watchlist_source(
    config: AgentConfig, state_store: StateStore, logger: Logger
) -> WatchlistSource:
    if config.watchlist_source == "notion":
        if config.notion is None:
            raise ValueError("notion config section is required for watchlist_source: notion")
        return NotionWatchlistSource(config.notion, state_store, logger)
    return StaticWatchlistSource(config)
