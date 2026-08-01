"""Notion-backed watchlist source (Requirements 1.3-1.6).

Queries the user's existing portfolio database directly over the Notion REST
API -- no SDK, no MCP -- so it works unchanged on a bare GitHub Actions runner.
Every successful fetch is cached in the StateStore; a failed fetch falls back
to that cache rather than proceeding with an empty watchlist.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import requests

from ..logger import Logger
from ..models import NotionConfig, TickerEntry
from ..state_store.base import KEY_WATCHLIST_CACHE, StateStore
from .base import MissingCredentialError, WatchlistSource, WatchlistUnavailable

COMPONENT = "NotionWatchlistSource"

NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"  # pinned: newer versions change the query endpoint shape
REQUEST_TIMEOUT_SECONDS = 30
PAGE_SIZE = 100


class NotionWatchlistSource(WatchlistSource):
    def __init__(
        self,
        config: NotionConfig,
        state_store: StateStore,
        logger: Logger,
        token: Optional[str] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._config = config
        self._store = state_store
        self._logger = logger
        self._token = token if token is not None else os.environ.get("NOTION_API_TOKEN", "")
        self._session = session or requests.Session()

    # -- public ------------------------------------------------------------

    def fetch(self) -> list[TickerEntry]:
        # A missing credential is a configuration error (exit 1), not a
        # transient outage -- it must not be papered over by the cache.
        if not self._token:
            raise MissingCredentialError(
                "NOTION_API_TOKEN is not set (required for watchlist_source: notion)"
            )
        try:
            pages = self._query_all_pages()
        except Exception as exc:  # noqa: BLE001 - any failure means "fall back to cache"
            self._logger.warning(
                COMPONENT,
                f"Notion watchlist query failed ({type(exc).__name__}: {exc}); "
                "falling back to the cached watchlist",
                error_type=type(exc).__name__,
            )
            return self._read_cache()

        entries = self._pages_to_entries(pages)
        self._write_cache(entries)
        self._logger.info(
            COMPONENT,
            f"Resolved {len(entries)} ticker(s) from Notion",
            database_id=self._config.database_id,
            pages_returned=len(pages),
        )
        return entries

    # -- HTTP --------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        }

    def _query_all_pages(self) -> list[dict[str, Any]]:
        url = f"{NOTION_API_BASE}/databases/{self._config.database_id}/query"
        headers = self._headers()
        server_side_filter: Optional[dict[str, Any]] = {
            "property": self._config.include_property,
            "checkbox": {"equals": True},
        }

        pages: list[dict[str, Any]] = []
        cursor: Optional[str] = None
        while True:
            payload: dict[str, Any] = {"page_size": PAGE_SIZE}
            if server_side_filter is not None:
                payload["filter"] = server_side_filter
            if cursor:
                payload["start_cursor"] = cursor

            response = self._session.post(
                url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT_SECONDS
            )

            if response.status_code == 400 and server_side_filter is not None:
                # Usually "Could not find property with name or id" -- the
                # checkbox has not been added to the database yet. Re-query
                # unfiltered and filter client-side (Requirement 1.3).
                self._logger.warning(
                    COMPONENT,
                    f"Notion rejected the server-side filter on "
                    f"{self._config.include_property!r}; retrying unfiltered and filtering "
                    f"client-side ({_short(response.text)})",
                    include_property=self._config.include_property,
                )
                server_side_filter = None
                cursor = None
                pages = []
                continue

            response.raise_for_status()
            body = response.json()
            pages.extend(body.get("results", []))

            if not body.get("has_more"):
                return pages
            cursor = body.get("next_cursor")
            if not cursor:
                return pages

    # -- parsing -----------------------------------------------------------

    def _pages_to_entries(self, pages: list[dict[str, Any]]) -> list[TickerEntry]:
        pattern = re.compile(self._config.ticker_pattern)
        entries: list[TickerEntry] = []
        missing_include_property = 0

        for page in pages:
            properties = page.get("properties") or {}
            include_prop = properties.get(self._config.include_property)
            if include_prop is None:
                missing_include_property += 1
                continue
            if not _checkbox_value(include_prop):
                continue

            title = _title_text(properties.get(self._config.title_property))
            match = pattern.search(title or "")
            if not match:
                self._logger.warning(
                    COMPONENT,
                    f"Notion page title {title!r} contains no parseable ticker; excluding it "
                    "from the watchlist",
                    page_url=page.get("url"),
                    page_id=page.get("id"),
                    title_property=self._config.title_property,
                )
                continue

            symbol = (match.group(1) if match.groups() else match.group(0)).upper()
            group = None
            if self._config.group_property:
                group = _property_text(properties.get(self._config.group_property))
            entries.append(TickerEntry(symbol=symbol, group=group))

        if missing_include_property:
            self._logger.warning(
                COMPONENT,
                f"{missing_include_property} Notion page(s) have no "
                f"{self._config.include_property!r} property; add that Checkbox property to the "
                "database (or set notion.include_property) or nothing will be watched",
                include_property=self._config.include_property,
            )

        return entries

    # -- cache -------------------------------------------------------------

    def _write_cache(self, entries: list[TickerEntry]) -> None:
        try:
            blob = json.dumps([e.to_dict() for e in entries]).encode("utf-8")
            self._store.set(KEY_WATCHLIST_CACHE, blob)  # no TTL: overwritten on each success
        except Exception as exc:  # noqa: BLE001 - caching is best-effort
            self._logger.warning(COMPONENT, f"Could not cache the Notion watchlist: {exc}")

    def _read_cache(self) -> list[TickerEntry]:
        try:
            blob = self._store.get(KEY_WATCHLIST_CACHE)
        except Exception as exc:  # noqa: BLE001
            raise WatchlistUnavailable(
                f"Notion query failed and the watchlist cache is unreadable: {exc}"
            ) from exc

        if not blob:
            raise WatchlistUnavailable(
                "Notion query failed and no cached watchlist exists; skipping this run"
            )

        entries = [TickerEntry.from_dict(item) for item in json.loads(blob.decode("utf-8"))]
        self._logger.info(
            COMPONENT, f"Using cached watchlist of {len(entries)} ticker(s)", cached=True
        )
        return entries


# --------------------------------------------------------------------------
# Notion property readers
# --------------------------------------------------------------------------


def _checkbox_value(prop: dict[str, Any]) -> bool:
    if prop.get("type") == "formula":
        return bool((prop.get("formula") or {}).get("boolean"))
    return bool(prop.get("checkbox"))


def _title_text(prop: Optional[dict[str, Any]]) -> str:
    if not prop:
        return ""
    rich = prop.get("title") or prop.get("rich_text") or []
    return "".join(part.get("plain_text", "") for part in rich)


def _property_text(prop: Optional[dict[str, Any]]) -> Optional[str]:
    """Read a select / status / multi-select / rich-text property as a label."""
    if not prop:
        return None
    prop_type = prop.get("type")
    if prop_type in ("select", "status"):
        value = prop.get(prop_type) or {}
        return value.get("name")
    if prop_type == "multi_select":
        names = [item.get("name") for item in prop.get("multi_select") or [] if item.get("name")]
        return ", ".join(names) or None
    if prop_type in ("rich_text", "title"):
        return _title_text(prop) or None
    if prop_type == "formula":
        return (prop.get("formula") or {}).get("string")
    return None


def _short(text: str, limit: int = 200) -> str:
    text = (text or "").replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."
