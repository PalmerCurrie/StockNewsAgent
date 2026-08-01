"""Alert assembly, ordering, and quiet-hours merge (Requirement 6)."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .dedup_filter import compute_identity_key
from .models import Alert, Event

COMPONENT = "AlertBuilder"

EARNINGS_HORIZON_DAYS = 7


def sort_events(events: list[Event]) -> list[Event]:
    """Impact desc, then |price movement| desc, then ticker asc (Requirement 6.2)."""
    return sorted(
        events,
        key=lambda e: (-e.impact_score, -abs(e.price_movement or 0.0), e.ticker),
    )


def group_events(events: list[Event]) -> dict[Optional[str], dict[str, list[Event]]]:
    """``{group_label: {ticker: [events]}}``, preserving the sorted order."""
    grouped: dict[Optional[str], dict[str, list[Event]]] = {}
    for event in events:
        by_ticker = grouped.setdefault(event.group, {})
        by_ticker.setdefault(event.ticker, []).append(event)
    return grouped


class AlertBuilder:
    def __init__(self, timezone_name: str) -> None:
        self._tz = ZoneInfo(timezone_name)

    def build(
        self,
        run_id: str,
        events: list[Event],
        tickers_monitored: int,
        earnings: dict[str, Optional[date]],
        now: Optional[datetime] = None,
        no_events_summary: Optional[str] = None,
    ) -> Alert:
        run_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        run_local = run_utc.astimezone(self._tz)

        for event in events:
            if not event.identity_key:
                event.identity_key = compute_identity_key(event)

        return Alert(
            run_id=run_id,
            run_timestamp_utc=run_utc,
            run_timestamp_local=run_local,
            tickers_monitored=tickers_monitored,
            events=sort_events(events),
            earnings_upcoming=self._upcoming_earnings(earnings, run_local.date()),
            no_events_summary=no_events_summary if not events else None,
        )

    def merge(self, current: Alert, suppressed: Optional[Alert]) -> Alert:
        """Fold a quiet-hours holdover into this run's Alert (Requirement 8.3)."""
        if suppressed is None:
            return current

        seen = {e.identity_key or compute_identity_key(e) for e in current.events}
        merged = list(current.events)
        for event in suppressed.events:
            key = event.identity_key or compute_identity_key(event)
            if key in seen:
                continue
            seen.add(key)
            merged.append(event)

        earnings = dict(suppressed.earnings_upcoming)
        earnings.update(current.earnings_upcoming)

        return Alert(
            run_id=current.run_id,
            run_timestamp_utc=current.run_timestamp_utc,
            run_timestamp_local=current.run_timestamp_local,
            tickers_monitored=max(current.tickers_monitored, suppressed.tickers_monitored),
            events=sort_events(merged),
            earnings_upcoming=earnings,
            no_events_summary=current.no_events_summary if not merged else None,
        )

    @staticmethod
    def _upcoming_earnings(
        earnings: dict[str, Optional[date]], today: date
    ) -> dict[str, date]:
        """Requirement 6.5 / Property 33 -- inclusive 0..7 day window."""
        horizon = today + timedelta(days=EARNINGS_HORIZON_DAYS)
        return {
            ticker: day
            for ticker, day in sorted(earnings.items())
            if day is not None and today <= day <= horizon
        }
