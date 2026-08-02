"""Data models for the Stock News Agent.

Every component communicates through these dataclasses -- there is no shared
mutable state anywhere in the pipeline. Models that cross a persistence or
process boundary (StateStore blobs, backtest fixtures, dry-run stdout) carry
explicit ``to_dict``/``from_dict`` pairs so the encoding is stable and
reviewable rather than whatever ``dataclasses.asdict`` happens to emit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from typing import Any, Optional


class RunMode(str, Enum):
    """Operational mode for a single invocation (Requirement 13)."""

    LIVE = "live"
    DRY_RUN = "dry_run"
    TEST_CHANNELS = "test_channels"
    BACKTEST = "backtest"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


# --------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------


def _dt_to_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _iso_to_dt(value: Optional[str]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _iso_to_date(value: Any) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


# --------------------------------------------------------------------------
# Configuration models
# --------------------------------------------------------------------------


@dataclass
class TickerEntry:
    """One watchlist entry."""

    symbol: str
    group: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "group": self.group}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TickerEntry":
        return cls(symbol=data["symbol"], group=data.get("group"))


@dataclass
class StateStoreConfig:
    type: str = "sqlite"  # "sqlite" (default) | "redis" (ephemeral hosts) | "memory" (tests)
    path: Optional[str] = "./agent_state.db"  # sqlite only


@dataclass
class ModelPricing:
    input_per_1m_usd: float
    output_per_1m_usd: float


@dataclass
class CostConfig:
    max_input_tokens_per_run: int = 100_000
    daily_cost_cap_usd: float = 1.00
    model_pricing: dict[str, ModelPricing] = field(default_factory=dict)


@dataclass
class QuietHoursConfig:
    start: str  # "HH:MM", 24-hour, in the configured timezone
    end: str


@dataclass
class ChannelConfig:
    """A configured delivery channel.

    Connection parameters are deliberately absent: they are read from
    environment variables at dispatch time (Requirement 10.3).
    """

    type: str  # "telegram" | "discord" | "email"


@dataclass
class AgentConfig:
    timezone: str = "America/New_York"
    watchlist_file: str = "watchlist.yaml"
    watchlist: Optional[list[TickerEntry]] = None
    lookback_window_hours: int = 24
    price_window_hours: int = 2
    llm_model: str = "claude-opus-5"
    llm_provider: Optional[str] = None  # "anthropic" | "openai"; inferred from the model
    llm_effort: str = "medium"  # Anthropic only: low | medium | high | xhigh | max
    llm_max_output_tokens: int = 8000  # caps thinking + response text on Claude models
    impact_threshold: int = 6
    high_impact_categories: list[str] = field(default_factory=list)
    benchmark_index: str = "SPY"
    active_hours_start: str = "09:30"
    active_hours_end: str = "17:00"
    quiet_hours: Optional[QuietHoursConfig] = None
    channels: list[ChannelConfig] = field(default_factory=list)
    news_sources: list[str] = field(default_factory=list)
    state_store: StateStoreConfig = field(default_factory=StateStoreConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    already_alerted_ttl_hours: int = 168


# --------------------------------------------------------------------------
# Market data models
# --------------------------------------------------------------------------


@dataclass
class OHLCVBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": _dt_to_iso(self.timestamp),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OHLCVBar":
        return cls(
            timestamp=_iso_to_dt(data["timestamp"]),
            open=float(data["open"]),
            high=float(data["high"]),
            low=float(data["low"]),
            close=float(data["close"]),
            volume=int(data.get("volume") or 0),
        )


@dataclass
class PriceData:
    ticker: str
    intraday: list[OHLCVBar] = field(default_factory=list)
    daily: list[OHLCVBar] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "intraday": [b.to_dict() for b in self.intraday],
            "daily": [b.to_dict() for b in self.daily],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PriceData":
        return cls(
            ticker=data["ticker"],
            intraday=[OHLCVBar.from_dict(b) for b in data.get("intraday", [])],
            daily=[OHLCVBar.from_dict(b) for b in data.get("daily", [])],
        )


@dataclass
class PriceMovementResult:
    value: Optional[float]  # percentage change over the price window
    pending: bool = False  # next session has not opened yet; reconcile later
    truncated: bool = False  # window extended past available data


# --------------------------------------------------------------------------
# News / event models
# --------------------------------------------------------------------------


@dataclass
class Story:
    ticker: str
    headline: str
    url: str  # source name when the adapter gave us no URL (Req 4.5)
    published_at: datetime
    source: str
    word_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "headline": self.headline,
            "url": self.url,
            "published_at": _dt_to_iso(self.published_at),
            "source": self.source,
            "word_count": self.word_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Story":
        headline = data.get("headline", "")
        return cls(
            ticker=data["ticker"],
            headline=headline,
            url=data.get("url") or data.get("source", ""),
            published_at=_iso_to_dt(data["published_at"]),
            source=data.get("source", ""),
            word_count=int(data.get("word_count") or len(headline.split())),
        )


@dataclass
class Event:
    ticker: str
    headline: str
    summary: str  # <= 150 words
    category: str
    impact_score: int  # 0-10
    source_urls: list[str]
    identity_key: str = ""  # sha256, computed by DedupFilter
    group: Optional[str] = None
    price_movement: Optional[float] = None
    price_movement_pending: bool = False
    price_window_hours: int = 2
    market_reaction: Optional[str] = None
    published_at: Optional[datetime] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "group": self.group,
            "headline": self.headline,
            "summary": self.summary,
            "category": self.category,
            "impact_score": self.impact_score,
            "source_urls": list(self.source_urls),
            "identity_key": self.identity_key,
            "price_movement": self.price_movement,
            "price_movement_pending": self.price_movement_pending,
            "price_window_hours": self.price_window_hours,
            "market_reaction": self.market_reaction,
            "published_at": _dt_to_iso(self.published_at),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(
            ticker=data["ticker"],
            group=data.get("group"),
            headline=data["headline"],
            summary=data.get("summary", ""),
            category=data.get("category", "other"),
            impact_score=int(data.get("impact_score") or 0),
            source_urls=list(data.get("source_urls") or []),
            identity_key=data.get("identity_key", ""),
            price_movement=data.get("price_movement"),
            price_movement_pending=bool(data.get("price_movement_pending")),
            price_window_hours=int(data.get("price_window_hours") or 2),
            market_reaction=data.get("market_reaction"),
            published_at=_iso_to_dt(data.get("published_at")),
        )


@dataclass
class Alert:
    run_id: str
    run_timestamp_utc: datetime
    run_timestamp_local: datetime
    tickers_monitored: int
    events: list[Event] = field(default_factory=list)
    earnings_upcoming: dict[str, date] = field(default_factory=dict)
    no_events_summary: Optional[str] = None  # <= 50 words (Req 5.6)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_timestamp_utc": _dt_to_iso(self.run_timestamp_utc),
            "run_timestamp_local": _dt_to_iso(self.run_timestamp_local),
            "tickers_monitored": self.tickers_monitored,
            "events": [e.to_dict() for e in self.events],
            "earnings_upcoming": {t: d.isoformat() for t, d in self.earnings_upcoming.items()},
            "no_events_summary": self.no_events_summary,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Alert":
        return cls(
            run_id=data["run_id"],
            run_timestamp_utc=_iso_to_dt(data["run_timestamp_utc"]),
            run_timestamp_local=_iso_to_dt(data["run_timestamp_local"]),
            tickers_monitored=int(data.get("tickers_monitored") or 0),
            events=[Event.from_dict(e) for e in data.get("events", [])],
            earnings_upcoming={
                t: _iso_to_date(d) for t, d in (data.get("earnings_upcoming") or {}).items()
            },
            no_events_summary=data.get("no_events_summary"),
        )


# --------------------------------------------------------------------------
# Delivery / run bookkeeping
# --------------------------------------------------------------------------


@dataclass
class DeliveryResult:
    channel_type: str
    status: str  # "success" | "failure" | "skipped"
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"channel_type": self.channel_type, "status": self.status, "error": self.error}


@dataclass
class RunSummary:
    run_id: str
    mode: str
    severity: str
    run_start_time: datetime
    run_end_time: datetime
    tickers_processed: list[str] = field(default_factory=list)
    stories_fetched_per_source: dict[str, int] = field(default_factory=dict)
    events_identified: int = 0
    events_dispatched: int = 0
    events_skipped_already_alerted: int = 0
    stories_skipped_already_alerted: int = 0  # dropped before the LLM saw them
    delivery_channel_statuses: list[DeliveryResult] = field(default_factory=list)
    llm_cost_usd_this_run: float = 0.0
    daily_cost_usd_running_total: float = 0.0
    errors: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "mode": self.mode,
            "severity": self.severity,
            "run_start_time": _dt_to_iso(self.run_start_time),
            "run_end_time": _dt_to_iso(self.run_end_time),
            "tickers_processed": list(self.tickers_processed),
            "stories_fetched_per_source": dict(self.stories_fetched_per_source),
            "events_identified": self.events_identified,
            "events_dispatched": self.events_dispatched,
            "events_skipped_already_alerted": self.events_skipped_already_alerted,
            "stories_skipped_already_alerted": self.stories_skipped_already_alerted,
            "delivery_channel_statuses": [d.to_dict() for d in self.delivery_channel_statuses],
            "llm_cost_usd_this_run": round(self.llm_cost_usd_this_run, 6),
            "daily_cost_usd_running_total": round(self.daily_cost_usd_running_total, 6),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class LockToken:
    """Handle returned by ``StateStore.acquire_lock``; required to release."""

    key: str
    token: str
    acquired_at: datetime
