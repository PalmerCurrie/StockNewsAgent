"""Active-hours and quiet-hours gating (Requirements 2.2, 8.1), plus the
exact-delivery hold.

Pure logic: no persistence, no I/O. Suppressed-alert storage is the Agent's
job, via the StateStore, and the sleep itself belongs to the Agent -- this
module only says how long to wait.
"""

from __future__ import annotations

from datetime import datetime, time, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from .models import AgentConfig

#: Upper bound on a --deliver-at hold. The intended gap is small (trigger at
#: 07:45 for an 08:00 delivery, minus the couple of minutes the run itself
#: takes). A much larger wait means the trigger fired at the wrong time, and
#: sleeping for hours would both burn Actions minutes and deliver news that
#: went stale while we slept. Past this bound the alert goes out immediately.
MAX_HOLD_SECONDS = 30 * 60


def parse_hhmm(value: str) -> time:
    hour, _, minute = value.partition(":")
    return time(hour=int(hour), minute=int(minute))


def _within(now: time, start: time, end: time) -> bool:
    """Half-open [start, end). A start == end range means "always"."""
    if start == end:
        return True
    if start < end:
        return start <= now < end
    # Midnight-spanning, e.g. 22:00-06:00.
    return now >= start or now < end


class TimeGate:
    def __init__(self, config: AgentConfig) -> None:
        self._tz = ZoneInfo(config.timezone)
        self._active_start = parse_hhmm(config.active_hours_start)
        self._active_end = parse_hhmm(config.active_hours_end)
        self._quiet = config.quiet_hours

    @property
    def timezone(self) -> ZoneInfo:
        return self._tz

    def to_local(self, now: Optional[datetime] = None) -> datetime:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(self._tz)

    def in_active_hours(self, now: Optional[datetime] = None) -> bool:
        return _within(self.to_local(now).time(), self._active_start, self._active_end)

    def in_quiet_hours(self, now: Optional[datetime] = None) -> bool:
        if self._quiet is None:
            return False
        start = parse_hhmm(self._quiet.start)
        end = parse_hhmm(self._quiet.end)
        if start == end:
            # A zero-width quiet range suppresses nothing (Requirement 8.4).
            return False
        return _within(self.to_local(now).time(), start, end)


class DeliveryHold:
    """Pins the dispatch to an exact wall-clock time.

    A run takes anywhere from ~2 to ~8 minutes depending on how much news there
    is, so even a perfectly punctual trigger delivers across a multi-minute
    window. Holding the dispatch until a fixed local time absorbs that spread:
    trigger early enough that the work is certainly finished, then wait out the
    remainder so the message lands on the minute.

    The hold sits *after* the fetch rather than before it, so waiting never
    costs freshness -- the news is gathered at trigger time and only the send
    is deferred.

    Being timezone-aware rather than UTC also means DST needs no seasonal
    edit: "08:00 America/Los_Angeles" is 08:00 in both PST and PDT.
    """

    def __init__(self, at: str, tz: str) -> None:
        self.at = parse_hhmm(at)
        self.tz = ZoneInfo(tz)

    def seconds_until(self, now: Optional[datetime] = None) -> float:
        """Seconds from ``now`` until today's target. Negative once it passes."""
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        local = moment.astimezone(self.tz)
        # combine() resolves the wall time against the zone's offset for that
        # date, which .replace() on an aware datetime would not.
        target = datetime.combine(local.date(), self.at, tzinfo=self.tz)
        return (target - local).total_seconds()

    def __str__(self) -> str:
        return f"{self.at.strftime('%H:%M')} {self.tz.key}"
