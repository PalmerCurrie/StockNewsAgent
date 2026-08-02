"""Offline plumbing shared by the adapter snapshot tests (Requirement 14.1).

Every test in this package parses a payload captured from the live source and
checked into ``tests/fixtures/news/``. Nothing here touches the network: the
HTTP adapters are handed a session that replays a fixture, and the yfinance
adapter has its ``Ticker`` swapped out.

The point of these tests is coverage, not just shape. An upstream format change
that makes an adapter drop rows must turn a test red rather than quietly
shrinking the story count, so every adapter asserts the zero-parse-failure
invariant in ``assert_full_coverage``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.news_fetcher import AdapterResult
from agent.models import Story

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "news"

TICKER = "AAPL"  # every fixture was captured for AAPL


def fixture_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def fixture_json(name: str) -> Any:
    return json.loads(fixture_bytes(name).decode("utf-8"))


class ReplayResponse:
    """The slice of ``requests.Response`` the adapters actually use."""

    def __init__(self, payload: bytes) -> None:
        self.content = payload
        self.status_code = 200

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def raise_for_status(self) -> None:
        return None


class ReplaySession:
    """Stands in for ``requests.Session`` and serves one saved payload."""

    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self.requested_urls: list[str] = []

    def get(self, url: str, **kwargs: Any) -> ReplayResponse:
        self.requested_urls.append(url)
        return ReplayResponse(self._payload)


def replay_session(fixture_name: str) -> ReplaySession:
    return ReplaySession(fixture_bytes(fixture_name))


# --------------------------------------------------------------------------
# Assertions
# --------------------------------------------------------------------------


def assert_full_coverage(result: AdapterResult, expected_items: int) -> None:
    """Requirement 14.2 -- every item in the fixture must become a Story.

    This is the assertion the 2026-07-28 Finviz timestamp change would have
    tripped. A source that drifts drops rows silently; here it fails loudly,
    with the first unparseable payload in the message.
    """
    assert result.total_items == expected_items, (
        f"fixture yielded {result.total_items} raw items, expected {expected_items} "
        "-- re-capture the fixture or fix the adapter"
    )
    assert result.parse_failures == 0, (
        f"{result.parse_failures}/{result.total_items} items failed to parse; "
        f"first failure sample: {result.failure_sample!r}"
    )
    assert len(result.stories) == expected_items, (
        f"parsed {len(result.stories)} stories from {expected_items} items"
    )


def assert_story_fields(stories: list[Story], source: str) -> None:
    """Every Story carries the association fields the pipeline depends on."""
    for index, story in enumerate(stories):
        context = f"{source} story #{index}"
        assert story.ticker == TICKER, context
        assert story.source == source, context
        assert story.headline.strip(), f"{context} has an empty headline"
        assert story.url and story.url != source, f"{context} fell back to the source name"
        # Site-relative hrefs are accepted because Finviz emits them for its own
        # news pages and the adapter stores them unresolved -- see
        # test_finviz_adapter.test_site_relative_hrefs_are_stored_unresolved.
        assert story.url.startswith(("http://", "https://", "/")), (
            f"{context} url is {story.url!r}"
        )
        assert story.published_at is not None, context
        assert story.published_at.tzinfo is not None, f"{context} timestamp is naive"
        assert story.word_count > 0, context


def snapshot(story: Story) -> dict[str, Any]:
    """A Story as a plain dict, for exact-match snapshot comparison.

    Comparing dicts rather than fields makes pytest print a per-key diff when a
    source renames or drops a field (Requirement 14.2).
    """
    return story.to_dict()
