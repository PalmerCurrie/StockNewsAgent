# Saved sample payloads, one per news adapter (Requirement 14.1).

All of these were captured live from the real sources for `AAPL` on 2026-08-01
and are replayed offline by `tests/snapshot/`. Nothing in that package touches
the network.

| File | Source | Captured as |
| --- | --- | --- |
| `yfinance_sample.json` | `yf.Ticker("AAPL").news` | verbatim, all 10 items |
| `google_news_sample.xml` | `news.google.com/rss/search?q=AAPL+stock` | verbatim channel header + the first 25 of 94 `<item>` elements |
| `yahoo_rss_sample.xml` | `feeds.finance.yahoo.com/rss/2.0/headline?s=AAPL` | verbatim, all 20 items |
| `finviz_sample.html` | `finviz.com/quote.ashx?t=AAPL` | the `#news-table` element verbatim (100 rows), in a minimal page wrapper |
| `finviz_relative_dates_sample.html` | derived from `finviz_sample.html` | the first 12 rows of the live markup with **only the timestamp cell text rewritten** |

`finviz_relative_dates_sample.html` is the one edited fixture. It reproduces the
2026-07-28 incident shape, which the live capture only shows a single row of:
`Today HH:MMPM` and `Yesterday HH:MMPM` rows, each followed by time-only rows
that inherit its date, sitting above the first absolutely-dated row. See the
docstring in `tests/snapshot/test_finviz_adapter.py`.

## Re-capturing

Refresh a fixture when a source legitimately changes shape -- never to make a
red test go green without first confirming the adapter still parses every row.
The snapshot tests pin exact `Story` values, so a re-capture means updating the
expected dicts in the matching `tests/snapshot/test_*_adapter.py` too.
