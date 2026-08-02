"""Snapshot tests for the Finviz adapter (Requirements 14.1, 14.2).

Payloads:

* ``finviz_sample.html`` -- the ``#news-table`` element from a live
  ``finviz.com/quote.ashx?t=AAPL`` page, saved verbatim (100 rows).
* ``finviz_relative_dates_sample.html`` -- the same markup with the timestamp
  cells relabelled to reproduce the 2026-07-28 incident (see below).

On 2026-07-28 Finviz started labelling the newest rows ``Today 07:55PM`` /
``Yesterday 04:30PM`` instead of ``Nov-25-24 08:00AM``. Rows are newest-first
and time-only rows inherit the date of the row above, so the unparsed labels
also left ``last_date`` unset for every time-only row above the first absolute
date: 117 of 300 rows were dropped, all of them the freshest, and the run
stayed green. ``test_relative_day_labels_drop_no_rows`` is the regression guard
-- it fails if any row is dropped, not merely if the count looks low.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

from agent.news_fetcher import FINVIZ_TIMEZONE, FinvizNewsAdapter

from .support import (
    TICKER,
    assert_full_coverage,
    assert_story_fields,
    replay_session,
    snapshot,
)

FIXTURE = "finviz_sample.html"
EXPECTED_ITEMS = 100

RELATIVE_FIXTURE = "finviz_relative_dates_sample.html"
RELATIVE_EXPECTED_ITEMS = 12

QUOTE_URL = "https://finviz.com/quote.ashx?t=AAPL"

SECOND_STORY = {
    "ticker": "AAPL",
    "headline": "South Korea exports beat forecasts in July on AI chip demand",
    "url": (
        "https://finance.yahoo.com/technology/ai/articles/"
        "south-korea-exports-beat-forecasts-030147951.html"
    ),
    "published_at": "2026-08-01T03:01:00+00:00",  # Jul-31-26 11:01PM ET
    "source": "finviz",
    "word_count": 11,
}

#: Rows 0-4 of the regression fixture, as (days before the Eastern "today",
#: Eastern clock time). Rows 1, 2 and 4 carry no day label at all -- they must
#: inherit the day from the ``Today`` / ``Yesterday`` row above them.
RELATIVE_ROWS = [
    (0, time(19, 55)),  # Today 07:55PM
    (0, time(18, 20)),  # 06:20PM
    (0, time(13, 10)),  # 01:10PM
    (1, time(16, 30)),  # Yesterday 04:30PM
    (1, time(11, 15)),  # 11:15AM
]

#: Rows 5-11, which carry (or inherit) absolute dates and so are fixed in UTC.
ABSOLUTE_ROWS = [
    "2026-07-31T02:13:00+00:00",  # Jul-30-26 10:13PM ET
    "2026-07-31T01:05:00+00:00",  # 09:05PM, inherited from Jul-30-26
    "2026-07-30T22:41:00+00:00",  # 06:41PM
    "2026-07-30T21:33:00+00:00",  # 05:33PM
    "2026-07-29T12:00:00+00:00",  # Jul-29-26 08:00AM ET
    "2026-07-29T11:12:00+00:00",  # 07:12AM, inherited from Jul-29-26
    "2026-07-29T10:02:00+00:00",  # 06:02AM
]


def _eastern_date(story):
    return story.published_at.astimezone(FINVIZ_TIMEZONE).date()


def test_parses_every_row_in_the_saved_page():
    session = replay_session(FIXTURE)

    result = FinvizNewsAdapter(session).fetch(TICKER)

    assert session.requested_urls == [QUOTE_URL]
    assert_full_coverage(result, EXPECTED_ITEMS)
    assert_story_fields(result.stories, "finviz")


def test_second_story_matches_snapshot():
    result = FinvizNewsAdapter(replay_session(FIXTURE)).fetch(TICKER)

    # Row 0 of the live capture is a "Today" row, whose date depends on the
    # clock; row 1 carries an absolute date and is stable.
    assert snapshot(result.stories[1]) == SECOND_STORY


def test_site_relative_hrefs_are_resolved_against_finviz():
    """4 of the 100 saved rows link to Finviz's own news pages with a
    site-relative href (``/news/375657/...``). Stored verbatim those are
    unclickable in a delivered alert, and invisible to ``deduplicate_stories``,
    which keys on the exact URL -- the same article arriving from another
    source under its canonical URL would not match.
    """
    result = FinvizNewsAdapter(replay_session(FIXTURE)).fetch(TICKER)

    assert [s.url for s in result.stories if s.url.startswith("https://finviz.com")] == [
        "https://finviz.com/news/375657/big-tech-earnings-fed-decision-shape-turbulent-week",
        "https://finviz.com/news/375649/dow-weathers-spiking-bond-yields-choppy-trading",
        "https://finviz.com/news/375618/optimistic-tech-earnings-lift-stock-futures",
        "https://finviz.com/news/375534/americas-rare-earth-comeback-is-gathering-serious-momentum",
    ]
    # No story escapes with a bare path, whatever the saved page contains.
    assert [s.url for s in result.stories if not s.url.startswith("https://")] == []


def test_newest_row_of_the_saved_page_uses_todays_eastern_date():
    before = datetime.now(FINVIZ_TIMEZONE).date()
    result = FinvizNewsAdapter(replay_session(FIXTURE)).fetch(TICKER)
    after = datetime.now(FINVIZ_TIMEZONE).date()

    newest = result.stories[0].published_at.astimezone(FINVIZ_TIMEZONE)
    assert newest.date() in {before, after}  # "Today 02:47PM"
    assert newest.timetz().replace(tzinfo=None) == time(14, 47)


def test_relative_day_labels_drop_no_rows():
    """Regression guard for the 2026-07-28 silent-coverage incident."""
    before = datetime.now(FINVIZ_TIMEZONE).date()
    result = FinvizNewsAdapter(replay_session(RELATIVE_FIXTURE)).fetch(TICKER)
    after = datetime.now(FINVIZ_TIMEZONE).date()

    assert_full_coverage(result, RELATIVE_EXPECTED_ITEMS)
    assert_story_fields(result.stories, "finviz")

    for index, (days_back, clock) in enumerate(RELATIVE_ROWS):
        local = result.stories[index].published_at.astimezone(FINVIZ_TIMEZONE)
        expected_dates = {
            before - timedelta(days=days_back),
            after - timedelta(days=days_back),
        }
        assert local.date() in expected_dates, f"row {index} resolved to {local}"
        assert local.timetz().replace(tzinfo=None) == clock, f"row {index}"


def test_time_only_rows_inherit_the_date_above_them():
    """The freshest rows must not fall back onto an older row's date."""
    result = FinvizNewsAdapter(replay_session(RELATIVE_FIXTURE)).fetch(TICKER)
    stories = result.stories

    # Rows 1-2 belong to the "Today" row, row 4 to the "Yesterday" row.
    assert _eastern_date(stories[1]) == _eastern_date(stories[0])
    assert _eastern_date(stories[2]) == _eastern_date(stories[0])
    assert _eastern_date(stories[4]) == _eastern_date(stories[3])
    assert _eastern_date(stories[3]) == _eastern_date(stories[0]) - timedelta(days=1)

    # Rows 5-11 carry or inherit absolute dates, so they are fixed in UTC.
    assert [s.published_at.isoformat() for s in stories[5:]] == ABSOLUTE_ROWS

    # Newest-first ordering must survive parsing; a row silently inheriting the
    # wrong date would show up here as an out-of-order timestamp.
    timestamps = [s.published_at for s in stories]
    assert timestamps == sorted(timestamps, reverse=True)
