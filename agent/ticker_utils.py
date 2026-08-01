"""Ticker format validation and watchlist deduplication (Requirement 1.7, 1.10).

Applied uniformly to whatever ``WatchlistSource`` returns, so the static-file
and Notion paths converge on the same normalized watchlist.
"""

from __future__ import annotations

import re

from .models import TickerEntry

# 1-10 uppercase alphanumerics with an optional exchange suffix (e.g. ".L", ".TO").
TICKER_PATTERN = re.compile(r"^[A-Z0-9]{1,10}(\.[A-Z]{1,4})?$")

MIN_WATCHLIST_SIZE = 1
MAX_WATCHLIST_SIZE = 50


def is_valid_ticker(symbol: str) -> bool:
    if not isinstance(symbol, str):
        return False
    return TICKER_PATTERN.match(symbol) is not None


def validate_and_dedupe(
    entries: list[TickerEntry],
) -> tuple[list[TickerEntry], list[str]]:
    """Return ``(valid_deduped_entries, rejected_symbols)``.

    Deduplication is silent and retains the group label of the first
    occurrence; it is idempotent, since running it over an already-deduped
    list is a no-op.
    """
    seen: set[str] = set()
    valid: list[TickerEntry] = []
    rejected: list[str] = []

    for entry in entries:
        symbol = (entry.symbol or "").strip().upper()
        if not is_valid_ticker(symbol):
            rejected.append(entry.symbol)
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        valid.append(TickerEntry(symbol=symbol, group=entry.group))

    return valid, rejected
