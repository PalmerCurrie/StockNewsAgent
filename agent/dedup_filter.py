"""Cross-run Event deduplication (Requirement 11.3-11.5).

The identity key is deterministic and source-agnostic, so the same story
reaching us via Google News on one run and Finviz on the next collapses to one
key and is alerted exactly once.
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlsplit, urlunsplit

from .logger import Logger
from .models import Event, Story
from .state_store.base import KEY_ALREADY_ALERTED, KEY_ANALYZED_STORIES, StateStore

COMPONENT = "DedupFilter"

_TRACKING_PARAM_PREFIXES = ("utm_", "fbclid", "gclid", "mc_", "ref", "ocid")


def normalize_url(url: str) -> str:
    """Lowercase host/scheme, drop tracking params, fragments, trailing slash."""
    parts = urlsplit(url.strip())
    query = "&".join(
        f"{key}={value}"
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not key.lower().startswith(_TRACKING_PARAM_PREFIXES)
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), path, query, "")
    )


def _key_from_url(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def _key_from_headline(ticker: str, headline: str, published_date: str) -> str:
    basis = f"{ticker}{headline.strip().lower()}{published_date}"
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def compute_identity_key(event: Event) -> str:
    url = next((u for u in event.source_urls if u.lower().startswith(("http://", "https://"))), None)
    if url:
        return _key_from_url(url)
    published_date = (
        event.published_at.date().isoformat() if event.published_at else "unknown-date"
    )
    return _key_from_headline(event.ticker, event.headline, published_date)


def compute_story_key(story: Story) -> str:
    """The key an Event built from this story alone would carry.

    Lets already-alerted stories be dropped *before* they reach the LLM, rather
    than paying to summarize them and discarding the result afterwards.
    """
    if story.url.lower().startswith(("http://", "https://")):
        return _key_from_url(story.url)
    return _key_from_headline(story.ticker, story.headline, story.published_at.date().isoformat())


class DedupFilter:
    def __init__(
        self, state_store: StateStore, logger: Logger, ttl_hours: int = 168, read_only: bool = False
    ) -> None:
        self._store = state_store
        self._logger = logger
        self._ttl_seconds = ttl_hours * 3600
        self._read_only = read_only  # dry-run / backtest must not record dispatches
        #: How many Events the last filter() call dropped (for the run summary).
        self.skipped_already_alerted = 0
        #: How many Stories were dropped before reaching the LLM this run.
        self.stories_skipped_already_alerted = 0

    def filter_stories(self, stories: list[Story]) -> list[Story]:
        """Drop stories an earlier run already sent to the LLM.

        The lookback window is much longer than the gap between runs, so most
        of what a later run fetches has already been analyzed. Filtering here
        rather than after the LLM call is what keeps repeat runs cheap.

        This checks ``analyzed_stories``, not ``already_alerted``: a story that
        was analyzed and judged not worth alerting on is still paid for, and
        must not be re-bought on the next run.
        """
        if not stories:
            return []

        keys = [compute_story_key(story) for story in stories]
        try:
            seen = self._store.set_contains_many(KEY_ANALYZED_STORIES, keys)
        except Exception as exc:  # noqa: BLE001 - a store hiccup must not drop news
            self._logger.warning(COMPONENT, f"Could not check the analyzed-stories set: {exc}")
            return list(stories)

        kept = [story for story, key in zip(stories, keys) if key not in seen]
        self.stories_skipped_already_alerted += len(stories) - len(kept)
        return kept

    def filter(self, events: list[Event]) -> list[Event]:
        kept: list[Event] = []
        self.skipped_already_alerted = 0

        for event in events:
            if not event.identity_key:
                event.identity_key = compute_identity_key(event)
            if self._store.set_contains(KEY_ALREADY_ALERTED, event.identity_key):
                self.skipped_already_alerted += 1
                self._logger.info(
                    COMPONENT,
                    f"Skipping already-alerted event for {event.ticker}: {event.headline}",
                    ticker=event.ticker,
                    identity_key=event.identity_key,
                )
                continue
            kept.append(event)

        return kept

    def record_dispatched(self, events: list[Event]) -> None:
        if self._read_only:
            return
        keys = [event.identity_key or compute_identity_key(event) for event in events]
        if keys:
            self._store.add_to_set_many(KEY_ALREADY_ALERTED, keys, self._ttl_seconds)

    def record_analyzed(self, stories: list[Story]) -> None:
        """Mark stories as paid-for, so later runs do not re-buy the analysis.

        Called once the run has reached a conclusion about them -- including
        "nothing here was worth sending". Deliberately *not* called when every
        delivery channel failed, so an undelivered alert is retried rather than
        silently dropped.
        """
        if self._read_only or not stories:
            return
        keys = [compute_story_key(story) for story in stories]
        try:
            self._store.add_to_set_many(KEY_ANALYZED_STORIES, keys, self._ttl_seconds)
        except Exception as exc:  # noqa: BLE001 - bookkeeping must not kill a run
            self._logger.warning(COMPONENT, f"Could not record analyzed stories: {exc}")
