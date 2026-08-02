"""Snapshot test for the Yahoo Finance RSS adapter (Requirements 14.1, 14.2).

Payload: ``tests/fixtures/news/yahoo_rss_sample.xml`` -- a live capture of
``feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL``, saved verbatim.
"""

from __future__ import annotations

from agent.news_fetcher import YahooFinanceRSSAdapter

from .support import (
    TICKER,
    assert_full_coverage,
    assert_story_fields,
    replay_session,
    snapshot,
)

FIXTURE = "yahoo_rss_sample.xml"
EXPECTED_ITEMS = 20

FIRST_STORY = {
    "ticker": "AAPL",
    "headline": "Apple CEO sends strong warning on AI and price of Apple products",
    "url": (
        "https://www.thestreet.com/technology/"
        "apple-tim-cook-admits-ai-raises-price-products?.tsrc=rss"
    ),
    "published_at": "2026-08-01T20:07:00+00:00",
    "source": "yahoo_rss",
    "word_count": 65,
}


def test_parses_every_entry_in_the_saved_feed():
    session = replay_session(FIXTURE)

    result = YahooFinanceRSSAdapter(session).fetch(TICKER)

    assert session.requested_urls == [
        "https://feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL&region=US&lang=en-US"
    ]
    assert_full_coverage(result, EXPECTED_ITEMS)
    assert_story_fields(result.stories, "yahoo_rss")


def test_first_story_matches_snapshot():
    result = YahooFinanceRSSAdapter(replay_session(FIXTURE)).fetch(TICKER)

    assert snapshot(result.stories[0]) == FIRST_STORY
