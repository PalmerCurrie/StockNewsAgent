"""Snapshot test for the yfinance adapter (Requirements 14.1, 14.2).

Payload: ``tests/fixtures/news/yfinance_sample.json`` -- the list of dicts
``yf.Ticker("AAPL").news`` returned, saved verbatim. yfinance nests the real
fields under ``content``; if that shape moves again, the exact-match snapshot
below prints the changed keys.
"""

from __future__ import annotations

from agent import news_fetcher
from agent.news_fetcher import YFinanceNewsAdapter

from .support import (
    TICKER,
    assert_full_coverage,
    assert_story_fields,
    fixture_json,
    snapshot,
)

FIXTURE = "yfinance_sample.json"
EXPECTED_ITEMS = 10

FIRST_STORY = {
    "ticker": "AAPL",
    "headline": "Apple CEO sends strong warning on AI and price of Apple products",
    "url": "https://www.thestreet.com/technology/apple-tim-cook-admits-ai-raises-price-products",
    "published_at": "2026-08-01T20:07:00+00:00",
    "source": "yfinance",
    "word_count": 65,
}


class _FakeTicker:
    """Replaces ``yf.Ticker``; ``.news`` replays the saved payload."""

    requested: list[str] = []

    def __init__(self, symbol: str) -> None:
        _FakeTicker.requested.append(symbol)
        self.news = fixture_json(FIXTURE)


def _fetch(monkeypatch) -> "news_fetcher.AdapterResult":
    _FakeTicker.requested = []
    monkeypatch.setattr(news_fetcher.yf, "Ticker", _FakeTicker)
    return YFinanceNewsAdapter().fetch(TICKER)


def test_parses_every_item_in_the_saved_payload(monkeypatch):
    result = _fetch(monkeypatch)

    assert _FakeTicker.requested == [TICKER]
    assert_full_coverage(result, EXPECTED_ITEMS)
    assert_story_fields(result.stories, "yfinance")


def test_first_story_matches_snapshot(monkeypatch):
    result = _fetch(monkeypatch)

    assert snapshot(result.stories[0]) == FIRST_STORY
