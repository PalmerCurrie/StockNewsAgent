"""Price and earnings-calendar ingestion via yfinance (Requirement 3).

``calculate_price_movement`` is a module-level pure function: it takes bars and
timestamps and returns a result, with no I/O and no clock access, so the
anchoring rules in the design's table are testable in isolation.
"""

from __future__ import annotations

import contextlib
import math
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterator, Optional
from zoneinfo import ZoneInfo

import yfinance as yf

from .logger import Logger
from .models import OHLCVBar, PriceData, PriceMovementResult

COMPONENT = "IngestionModule"

MARKET_TIMEZONE = ZoneInfo("America/New_York")
REGULAR_OPEN = time(9, 30)
REGULAR_CLOSE = time(16, 0)

_OHLCV_FIELDS = ("Open", "High", "Low", "Close", "Volume")


@contextlib.contextmanager
def _quiet_stdout() -> Iterator[None]:
    """Send yfinance's chatter to stderr.

    yfinance prints things like ``HTTP Error 404: {...}`` straight to stdout,
    which lands in the middle of the alert JSON that --dry-run and --backtest
    write there. Keeping stdout machine-readable matters more than where the
    noise goes; stderr still shows it in the Actions log.
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


# --------------------------------------------------------------------------
# Pure price-movement logic
# --------------------------------------------------------------------------


def is_regular_hours(moment: datetime, market_tz: ZoneInfo = MARKET_TIMEZONE) -> bool:
    local = moment.astimezone(market_tz)
    if local.weekday() >= 5:
        return False
    return REGULAR_OPEN <= local.time() < REGULAR_CLOSE


def is_pre_market(moment: datetime, market_tz: ZoneInfo = MARKET_TIMEZONE) -> bool:
    local = moment.astimezone(market_tz)
    if local.weekday() >= 5:
        return False
    return local.time() < REGULAR_OPEN


def calculate_price_movement(
    prices: PriceData,
    event_time: datetime,
    window_hours: int,
    market_tz: ZoneInfo = MARKET_TIMEZONE,
) -> PriceMovementResult:
    """Percentage move attributable to an event, anchored per Requirement 3.4/3.5.

    | event published        | start anchor            | end anchor                    |
    |------------------------|-------------------------|-------------------------------|
    | regular hours          | close at/just-before it | close at event+window         |
    | pre-market             | prior session close     | current session open + window |
    | after-hours / weekend  | most recent close       | next session open + window    |
    | next session not open  | --                      | pending: true                 |
    """
    if event_time.tzinfo is None:
        event_time = event_time.replace(tzinfo=timezone.utc)

    intraday = sorted(prices.intraday, key=lambda b: b.timestamp)
    daily = sorted(prices.daily, key=lambda b: b.timestamp)
    window = timedelta(hours=window_hours)

    if is_regular_hours(event_time, market_tz):
        start_bar = _last_at_or_before(intraday, event_time)
        if start_bar is not None:
            window_end = event_time + window
            end_bar = _last_at_or_before(intraday, window_end) or start_bar
            truncated = intraday[-1].timestamp < window_end
            return _pct(start_bar.close, end_bar.close, truncated=truncated)
        # No intraday coverage for a regular-hours event: fall through to daily.
        return _daily_fallback(daily, event_time, market_tz)

    # Outside regular hours: anchor on a session close, end after the next open.
    local_event_date = event_time.astimezone(market_tz).date()
    if is_pre_market(event_time, market_tz):
        start_bar = _last_daily_before_date(daily, local_event_date)
    else:
        start_bar = _last_daily_on_or_before_date(daily, local_event_date)

    forward_bars = [bar for bar in intraday if bar.timestamp > event_time]
    if not forward_bars:
        # The relevant session has not opened yet -- a later run reconciles it.
        return PriceMovementResult(value=None, pending=True, truncated=False)

    if start_bar is None:
        start_bar = forward_bars[0]

    session_open = forward_bars[0].timestamp
    window_end = session_open + window
    end_bar = _last_at_or_before(forward_bars, window_end) or forward_bars[-1]
    truncated = forward_bars[-1].timestamp < window_end
    return _pct(start_bar.close, end_bar.close, truncated=truncated)


def _pct(start: float, end: float, truncated: bool) -> PriceMovementResult:
    if not start or math.isnan(start) or math.isnan(end):
        return PriceMovementResult(value=None, pending=False, truncated=truncated)
    return PriceMovementResult(
        value=round((end - start) / start * 100, 4), pending=False, truncated=truncated
    )


def _last_at_or_before(bars: list[OHLCVBar], moment: datetime) -> Optional[OHLCVBar]:
    candidates = [bar for bar in bars if bar.timestamp <= moment]
    return candidates[-1] if candidates else None


def _last_daily_before_date(bars: list[OHLCVBar], day: date) -> Optional[OHLCVBar]:
    candidates = [bar for bar in bars if bar.timestamp.astimezone(MARKET_TIMEZONE).date() < day]
    return candidates[-1] if candidates else None


def _last_daily_on_or_before_date(bars: list[OHLCVBar], day: date) -> Optional[OHLCVBar]:
    candidates = [bar for bar in bars if bar.timestamp.astimezone(MARKET_TIMEZONE).date() <= day]
    return candidates[-1] if candidates else None


def _daily_fallback(
    daily: list[OHLCVBar], event_time: datetime, market_tz: ZoneInfo
) -> PriceMovementResult:
    """Requirement 3.6 -- no intraday data, so use the most recent daily closes."""
    if len(daily) < 2:
        return PriceMovementResult(value=None, pending=False, truncated=False)
    event_date = event_time.astimezone(market_tz).date()
    start_bar = _last_daily_before_date(daily, event_date) or daily[0]
    end_bar = daily[-1]
    if end_bar.timestamp <= start_bar.timestamp:
        return PriceMovementResult(value=None, pending=False, truncated=False)
    return _pct(start_bar.close, end_bar.close, truncated=True)


# --------------------------------------------------------------------------
# yfinance wrapper
# --------------------------------------------------------------------------


class IngestionModule:
    def __init__(self, logger: Logger, market_tz: ZoneInfo = MARKET_TIMEZONE) -> None:
        self._logger = logger
        self._market_tz = market_tz

    def fetch_prices(self, tickers: list[str]) -> dict[str, PriceData]:
        """Intraday 5m bars for the current session + daily bars for 5 sessions."""
        if not tickers:
            return {}

        intraday = self._download(tickers, period="1d", interval="5m")
        daily = self._download(tickers, period="5d", interval="1d")

        result: dict[str, PriceData] = {}
        for ticker in tickers:
            intraday_bars = self._extract_bars(intraday, ticker, "5m")
            daily_bars = self._extract_bars(daily, ticker, "1d")
            if not intraday_bars and not daily_bars:
                self._logger.warning(
                    COMPONENT,
                    f"yfinance returned no price data for {ticker}; excluding it from this run",
                    ticker=ticker,
                )
                continue
            if not intraday_bars:
                self._logger.info(
                    COMPONENT,
                    f"No intraday data for {ticker}; falling back to daily closes",
                    ticker=ticker,
                )
            result[ticker] = PriceData(
                ticker=ticker, intraday=intraday_bars, daily=daily_bars
            )

        if not result:
            self._logger.error(
                COMPONENT,
                "No valid tickers remain after price ingestion",
                error_type="NoValidTickers",
                tickers=tickers,
            )
        return result

    def fetch_earnings_calendar(self, tickers: list[str]) -> dict[str, Optional[date]]:
        calendar: dict[str, Optional[date]] = {}
        for ticker in tickers:
            try:
                with _quiet_stdout():
                    raw = yf.Ticker(ticker).calendar
                calendar[ticker] = _parse_earnings_date(raw)
            except Exception as exc:  # noqa: BLE001 - per-ticker failures are non-fatal
                self._logger.warning(
                    COMPONENT,
                    f"Could not fetch the earnings calendar for {ticker}: {exc}",
                    ticker=ticker,
                    error_type=type(exc).__name__,
                )
                calendar[ticker] = None
        return calendar

    def calculate_price_movement(
        self, prices: PriceData, event_time: datetime, window_hours: int
    ) -> PriceMovementResult:
        result = calculate_price_movement(prices, event_time, window_hours, self._market_tz)
        if result.truncated:
            self._logger.info(
                COMPONENT,
                f"Price window for {prices.ticker} extends past available data; "
                "using the last available bar",
                ticker=prices.ticker,
                window_hours=window_hours,
            )
        return result

    # -- yfinance plumbing -------------------------------------------------

    def _download(self, tickers: list[str], period: str, interval: str) -> Any:
        try:
            with _quiet_stdout():
                return yf.download(
                    tickers=tickers,
                    period=period,
                    interval=interval,
                    group_by="ticker",
                    auto_adjust=False,
                    actions=False,
                    progress=False,
                    threads=True,
                )
        except Exception as exc:  # noqa: BLE001 - a failed batch is a warning, not a crash
            self._logger.warning(
                COMPONENT,
                f"yfinance download failed for interval {interval}: {exc}",
                interval=interval,
                error_type=type(exc).__name__,
            )
            return None

    def _extract_bars(self, frame: Any, ticker: str, interval: str) -> list[OHLCVBar]:
        if frame is None or getattr(frame, "empty", True):
            return []

        try:
            if hasattr(frame.columns, "levels") and frame.columns.nlevels > 1:
                if ticker not in frame.columns.get_level_values(0):
                    return []
                sub = frame[ticker]
            else:
                sub = frame
        except Exception:  # noqa: BLE001 - unexpected frame shape
            return []

        bars: list[OHLCVBar] = []
        missing_fields: set[str] = set()

        for timestamp, row in sub.iterrows():
            values = {}
            for field in _OHLCV_FIELDS:
                raw = row.get(field) if hasattr(row, "get") else None
                if raw is None or (isinstance(raw, float) and math.isnan(raw)):
                    missing_fields.add(field)
                    values[field] = None
                else:
                    values[field] = raw

            close = values["Close"]
            if close is None:
                # Yahoo intermittently returns a NaN Close on the most recent
                # daily bar; Adj Close is the next-best stand-in.
                adjusted = row.get("Adj Close") if hasattr(row, "get") else None
                if adjusted is not None and not (
                    isinstance(adjusted, float) and math.isnan(adjusted)
                ):
                    close = adjusted
                else:
                    continue  # a bar with no close at all is unusable

            bars.append(
                OHLCVBar(
                    timestamp=_to_utc(timestamp, self._market_tz),
                    open=float(values["Open"] if values["Open"] is not None else close),
                    high=float(values["High"] if values["High"] is not None else close),
                    low=float(values["Low"] if values["Low"] is not None else close),
                    close=float(close),
                    volume=int(values["Volume"] or 0),
                )
            )

        if missing_fields:
            self._logger.warning(
                COMPONENT,
                f"Missing OHLCV field(s) {sorted(missing_fields)} for {ticker} at interval "
                f"{interval}; using the available fields",
                ticker=ticker,
                interval=interval,
                missing_fields=sorted(missing_fields),
            )
        return bars


def _to_utc(timestamp: Any, market_tz: ZoneInfo) -> datetime:
    moment = timestamp.to_pydatetime() if hasattr(timestamp, "to_pydatetime") else timestamp
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=market_tz)
    return moment.astimezone(timezone.utc)


def _parse_earnings_date(raw: Any) -> Optional[date]:
    """yfinance has returned this as a dict and as a DataFrame across versions."""
    if raw is None:
        return None

    candidate: Any = None
    if isinstance(raw, dict):
        for key in ("Earnings Date", "earningsDate", "Earnings Average"):
            if key in raw:
                candidate = raw[key]
                break
    elif hasattr(raw, "loc"):
        try:
            candidate = raw.loc["Earnings Date"]
        except Exception:  # noqa: BLE001
            candidate = None

    if candidate is None:
        return None
    if isinstance(candidate, (list, tuple)):
        candidate = candidate[0] if candidate else None
    if candidate is None:
        return None
    if hasattr(candidate, "iloc"):
        try:
            candidate = candidate.iloc[0]
        except Exception:  # noqa: BLE001
            return None
    if isinstance(candidate, datetime):
        return candidate.date()
    if isinstance(candidate, date):
        return candidate
    try:
        return date.fromisoformat(str(candidate)[:10])
    except ValueError:
        return None
