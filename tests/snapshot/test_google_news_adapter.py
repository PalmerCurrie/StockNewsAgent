"""Snapshot test for the Google News RSS adapter (Requirements 14.1, 14.2).

Payload: ``tests/fixtures/news/google_news_sample.xml`` -- a live capture of
``news.google.com/rss/search?q=AAPL+stock``, trimmed to its first 25 ``<item>``
elements. Google's items carry RFC 2822 ``pubDate`` values and a redirect
``link``; both are what the adapter depends on.
"""

from __future__ import annotations

from agent.news_fetcher import GoogleNewsRSSAdapter

from .support import (
    TICKER,
    assert_full_coverage,
    assert_story_fields,
    replay_session,
    snapshot,
)

FIXTURE = "google_news_sample.xml"
EXPECTED_ITEMS = 25

FIRST_STORY = {
    "ticker": "AAPL",
    "headline": (
        "Apple stock plunges: $300 support hangs by a thread after guidance miss "
        "(AAPL:NASDAQ) - Seeking Alpha"
    ),
    "url": (
        "https://news.google.com/rss/articles/CBMiqwFBVV95cUxOVFlQOVVZYjBlWG1uMmZT"
        "SUxRallxeFhoM3QxVzVHZXRUcV9SVTh5UzMzbHFBRG1XQTB5VmdBMmoxNllkYmEyUWl6UXU2"
        "ZWF6WG4yMDhhMmYwU1lCRjB5QzlybnN3dV9BUGRxS0RabTBfTENoZjAtMFlDUFl6cmZPQ3JG"
        "XzhxNDZEd3M5OGRQNHZua0F0YjFTSE5MQzY3dDdUZ2R3UFAySkJKa1U?oc=5"
    ),
    "published_at": "2026-07-31T16:51:37+00:00",
    "source": "google_news",
    "word_count": 144,
}


def test_parses_every_entry_in_the_saved_feed():
    session = replay_session(FIXTURE)

    result = GoogleNewsRSSAdapter(session).fetch(TICKER)

    assert session.requested_urls == [
        "https://news.google.com/rss/search?q=AAPL+stock&hl=en-US&gl=US&ceid=US:en"
    ]
    assert_full_coverage(result, EXPECTED_ITEMS)
    assert_story_fields(result.stories, "google_news")


def test_first_story_matches_snapshot():
    result = GoogleNewsRSSAdapter(replay_session(FIXTURE)).fetch(TICKER)

    assert snapshot(result.stories[0]) == FIRST_STORY
