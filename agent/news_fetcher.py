"""Multi-source news fetching (Requirements 4, 14).

Each source is an adapter behind one small interface. Adapters never raise into
the pipeline: a dead source is a warning and the run continues with whatever
the other sources returned.
"""

from __future__ import annotations

import contextlib
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import feedparser
import requests
import yfinance as yf
from bs4 import BeautifulSoup

from .logger import Logger
from .models import Story

COMPONENT = "NewsFetcher"

TIMEOUT_SECONDS = 30
PARSE_FAILURE_WARN_RATIO = 0.10  # Requirement 14.3
USER_AGENT = "Mozilla/5.0 (compatible; StockNewsAgent/0.1; +https://github.com)"
FINVIZ_TIMEZONE = ZoneInfo("America/New_York")  # Finviz prints US/Eastern clock times


@dataclass
class AdapterResult:
    stories: list[Story] = field(default_factory=list)
    total_items: int = 0
    parse_failures: int = 0
    failure_sample: Optional[str] = None

    def note_failure(self, payload: Any) -> None:
        self.parse_failures += 1
        if self.failure_sample is None:
            self.failure_sample = str(payload)[:500]


class NewsSourceAdapter(ABC):
    name: str = "unknown"

    def __init__(self, session: Optional[requests.Session] = None) -> None:
        self._session = session or requests.Session()

    @abstractmethod
    def fetch(self, ticker: str) -> AdapterResult:
        """Fetch raw items for one ticker and parse them into Story objects."""

    def _get(self, url: str, **kwargs: Any) -> requests.Response:
        headers = {"User-Agent": USER_AGENT, **kwargs.pop("headers", {})}
        return self._session.get(url, headers=headers, timeout=TIMEOUT_SECONDS, **kwargs)


# --------------------------------------------------------------------------
# Adapters
# --------------------------------------------------------------------------


class YFinanceNewsAdapter(NewsSourceAdapter):
    name = "yfinance"

    def fetch(self, ticker: str) -> AdapterResult:
        result = AdapterResult()
        # yfinance prints HTTP errors to stdout, where --dry-run writes JSON.
        with contextlib.redirect_stdout(sys.stderr):
            items = yf.Ticker(ticker).news or []
        result.total_items = len(items)
        for item in items:
            try:
                story = self._parse(ticker, item)
            except Exception:  # noqa: BLE001 - one bad item must not kill the source
                story = None
            if story is None:
                result.note_failure(item)
            else:
                result.stories.append(story)
        return result

    def _parse(self, ticker: str, item: dict) -> Optional[Story]:
        # yfinance >= 0.2.5x nests the payload under "content".
        content = item.get("content") if isinstance(item.get("content"), dict) else item

        headline = content.get("title") or item.get("title")
        if not headline:
            return None

        url = (
            (content.get("canonicalUrl") or {}).get("url")
            if isinstance(content.get("canonicalUrl"), dict)
            else None
        ) or (
            (content.get("clickThroughUrl") or {}).get("url")
            if isinstance(content.get("clickThroughUrl"), dict)
            else None
        ) or item.get("link")

        published = content.get("pubDate") or content.get("displayTime")
        if published:
            published_at = _parse_timestamp(published)
        else:
            epoch = item.get("providerPublishTime")
            published_at = (
                datetime.fromtimestamp(int(epoch), tz=timezone.utc) if epoch else None
            )
        if published_at is None:
            return None

        summary = content.get("summary") or content.get("description") or ""
        return Story(
            ticker=ticker,
            headline=headline,
            url=url or self.name,
            published_at=published_at,
            source=self.name,
            word_count=len(f"{headline} {summary}".split()),
        )


class _RSSAdapter(NewsSourceAdapter):
    """Shared RSS parsing; subclasses only supply the feed URL."""

    def feed_url(self, ticker: str) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def fetch(self, ticker: str) -> AdapterResult:
        result = AdapterResult()
        response = self._get(self.feed_url(ticker))
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        entries = feed.entries or []
        result.total_items = len(entries)

        for entry in entries:
            try:
                headline = entry.get("title")
                published_at = _parse_timestamp(
                    entry.get("published") or entry.get("updated")
                ) or _from_struct_time(entry.get("published_parsed"))
                if not headline or published_at is None:
                    result.note_failure(entry)
                    continue
                summary = entry.get("summary", "")
                result.stories.append(
                    Story(
                        ticker=ticker,
                        headline=headline,
                        url=entry.get("link") or self.name,
                        published_at=published_at,
                        source=self.name,
                        word_count=len(f"{headline} {summary}".split()),
                    )
                )
            except Exception:  # noqa: BLE001
                result.note_failure(entry)
        return result


class GoogleNewsRSSAdapter(_RSSAdapter):
    name = "google_news"

    def feed_url(self, ticker: str) -> str:
        return (
            f"https://news.google.com/rss/search?q={ticker}+stock"
            "&hl=en-US&gl=US&ceid=US:en"
        )


class YahooFinanceRSSAdapter(_RSSAdapter):
    name = "yahoo_rss"

    def feed_url(self, ticker: str) -> str:
        return (
            f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={ticker}"
            "&region=US&lang=en-US"
        )


class FinvizNewsAdapter(NewsSourceAdapter):
    name = "finviz"

    def fetch(self, ticker: str) -> AdapterResult:
        result = AdapterResult()
        response = self._get(f"https://finviz.com/quote.ashx?t={ticker}")
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        table = soup.find(id="news-table")
        if table is None:
            result.note_failure(response.text)
            result.total_items = 1
            return result

        rows = table.find_all("tr")
        result.total_items = len(rows)
        last_date: Optional[datetime] = None

        for row in rows:
            try:
                cells = row.find_all("td")
                if len(cells) < 2:
                    result.note_failure(row)
                    continue
                published_at, last_date = _parse_finviz_timestamp(
                    cells[0].get_text(strip=True), last_date
                )
                link = cells[1].find("a")
                if published_at is None or link is None or not link.get_text(strip=True):
                    result.note_failure(row)
                    continue
                headline = link.get_text(strip=True)
                result.stories.append(
                    Story(
                        ticker=ticker,
                        headline=headline,
                        url=link.get("href") or self.name,
                        published_at=published_at,
                        source=self.name,
                        word_count=len(headline.split()),
                    )
                )
            except Exception:  # noqa: BLE001
                result.note_failure(row)
        return result


ADAPTERS: dict[str, type[NewsSourceAdapter]] = {
    YFinanceNewsAdapter.name: YFinanceNewsAdapter,
    GoogleNewsRSSAdapter.name: GoogleNewsRSSAdapter,
    FinvizNewsAdapter.name: FinvizNewsAdapter,
    YahooFinanceRSSAdapter.name: YahooFinanceRSSAdapter,
}


# --------------------------------------------------------------------------
# Fetcher
# --------------------------------------------------------------------------


class NewsFetcher:
    def __init__(
        self,
        source_names: list[str],
        logger: Logger,
        session: Optional[requests.Session] = None,
        adapters: Optional[list[NewsSourceAdapter]] = None,
    ) -> None:
        self._logger = logger
        if adapters is not None:
            self._adapters = adapters
        else:
            shared = session or requests.Session()
            self._adapters = [
                ADAPTERS[name](shared) for name in source_names if name in ADAPTERS
            ]
        #: Cumulative per-source counts for the run summary (Requirement 9.1).
        self.stories_per_source: dict[str, int] = {}

    def fetch_stories(
        self, ticker: str, lookback_hours: int, now: Optional[datetime] = None
    ) -> list[Story]:
        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=lookback_hours)

        collected: list[Story] = []
        failures = 0

        for adapter in self._adapters:
            try:
                result = adapter.fetch(ticker)
            except Exception as exc:  # noqa: BLE001 - Requirement 4.4
                failures += 1
                self._logger.warning(
                    COMPONENT,
                    f"News source {adapter.name!r} failed for {ticker}: {exc}",
                    adapter_name=adapter.name,
                    ticker=ticker,
                    error_type=type(exc).__name__,
                )
                continue

            self._warn_on_parse_drift(adapter.name, ticker, result)
            self.stories_per_source[adapter.name] = self.stories_per_source.get(
                adapter.name, 0
            ) + len(result.stories)
            collected.extend(result.stories)

        if self._adapters and failures == len(self._adapters):
            self._logger.error(
                COMPONENT,
                f"All configured news sources failed for {ticker}",
                error_type="AllSourcesFailed",
                ticker=ticker,
            )
            return []

        within_window = [s for s in collected if cutoff <= s.published_at <= now]
        return deduplicate_stories(within_window)

    def _warn_on_parse_drift(self, adapter_name: str, ticker: str, result: AdapterResult) -> None:
        """Requirement 14.3 -- loud warning when a source's shape drifts."""
        if not result.total_items or not result.parse_failures:
            return
        ratio = result.parse_failures / result.total_items
        if ratio < PARSE_FAILURE_WARN_RATIO:
            return
        self._logger.warning(
            COMPONENT,
            f"{result.parse_failures}/{result.total_items} items from {adapter_name!r} "
            f"failed to parse for {ticker}",
            adapter_name=adapter_name,
            ticker=ticker,
            parse_failure_count=result.parse_failures,
            total_items=result.total_items,
            sample=result.failure_sample,
        )


def deduplicate_stories(stories: list[Story]) -> list[Story]:
    """Requirement 4.7 -- exact URL first, then (ticker, headline, published_at)."""
    seen_urls: set[str] = set()
    seen_composites: set[tuple[str, str, str]] = set()
    deduped: list[Story] = []

    for story in stories:
        url_key = (story.url or "").strip().lower()
        if url_key and url_key in seen_urls:
            continue
        composite = (
            story.ticker,
            story.headline.strip().lower(),
            story.published_at.isoformat(),
        )
        if composite in seen_composites:
            continue
        if url_key:
            seen_urls.add(url_key)
        seen_composites.add(composite)
        deduped.append(story)

    return deduped


# --------------------------------------------------------------------------
# Timestamp parsing
# --------------------------------------------------------------------------


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = parsedate_to_datetime(text)  # RFC 2822, as used by RSS
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        iso = text[:-1] + "+00:00" if text.endswith("Z") else text
        parsed = datetime.fromisoformat(iso)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _from_struct_time(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime(*value[:6], tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _parse_finviz_timestamp(
    text: str, last_date: Optional[datetime], now: Optional[datetime] = None
) -> tuple[Optional[datetime], Optional[datetime]]:
    """Finviz prints 'Nov-25-24 08:00AM', then time-only for same-day rows.

    The newest rows carry a relative day label instead -- 'Today 07:55PM',
    'Yesterday 04:30PM'. Those must be resolved against the current Eastern
    date: rows are newest-first, so failing them also leaves ``last_date``
    unset for every time-only row above the first absolute date, which drops
    the whole of today's news from this source.

    Clock times are US/Eastern, so they are localized before being converted
    to UTC; treating them as UTC would shift every Finviz story by 4-5 hours
    and skew lookback filtering.
    """
    from datetime import datetime as _dt

    parts = text.replace("\xa0", " ").split()
    try:
        if len(parts) == 2:
            label = parts[0].lower()
            if label in ("today", "yesterday"):
                clock = _dt.strptime(parts[1], "%I:%M%p")
                day = (now or _dt.now(FINVIZ_TIMEZONE)).astimezone(FINVIZ_TIMEZONE).date()
                if label == "yesterday":
                    day -= timedelta(days=1)
                local = _dt(
                    day.year, day.month, day.day, clock.hour, clock.minute,
                    tzinfo=FINVIZ_TIMEZONE,
                )
            else:
                naive = _dt.strptime(f"{parts[0]} {parts[1]}", "%b-%d-%y %I:%M%p")
                local = naive.replace(tzinfo=FINVIZ_TIMEZONE)
            return local.astimezone(timezone.utc), local
        if len(parts) == 1 and last_date is not None:
            clock = _dt.strptime(parts[0], "%I:%M%p")
            local = last_date.replace(hour=clock.hour, minute=clock.minute)
            return local.astimezone(timezone.utc), last_date
    except ValueError:
        return None, last_date
    return None, last_date
