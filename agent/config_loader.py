"""Config loading, environment-variable overrides, and validation (Requirement 10).

Precedence is always: environment variable > config file > dataclass default.
Secrets never appear in the config file schema -- they are read directly from
the environment by the components that need them.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from croniter import croniter

from .models import (
    AgentConfig,
    ChannelConfig,
    CostConfig,
    ModelPricing,
    NotionConfig,
    QuietHoursConfig,
    StateStoreConfig,
    TickerEntry,
)
from .ticker_utils import (
    MAX_WATCHLIST_SIZE,
    MIN_WATCHLIST_SIZE,
    is_valid_ticker,
    validate_and_dedupe,
)

HHMM_PATTERN = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

SUPPORTED_CHANNEL_TYPES = {"telegram", "discord", "email"}
SUPPORTED_NEWS_SOURCES = {"yfinance", "google_news", "finviz", "yahoo_rss"}
SUPPORTED_STATE_STORES = {"redis", "sqlite", "memory"}
SUPPORTED_WATCHLIST_SOURCES = {"static", "notion"}
SUPPORTED_LLM_PROVIDERS = {"anthropic", "openai"}
SUPPORTED_LLM_EFFORTS = {"low", "medium", "high", "xhigh", "max"}

DEFAULT_CATEGORIES = [
    "earnings_release",
    "regulatory_ruling",
    "product_launch",
    "analyst_rating_change",
    "macroeconomic_announcement",
]


class ConfigError(Exception):
    """Unrecoverable configuration problem -- the Agent exits 1."""


def _as_int(value: str) -> int:
    return int(value)


def _as_float(value: str) -> float:
    return float(value)


def _as_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _as_channels(value: str) -> list[dict]:
    return [{"type": item} for item in _as_csv(value)]


# env var -> (dotted path into the raw config dict, parser)
ENV_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "AGENT_TIMEZONE": ("timezone", str),
    "AGENT_WATCHLIST_SOURCE": ("watchlist_source", str),
    "AGENT_LOOKBACK_WINDOW_HOURS": ("lookback_window_hours", _as_int),
    "AGENT_PRICE_WINDOW_HOURS": ("price_window_hours", _as_int),
    "AGENT_LLM_MODEL": ("llm_model", str),
    "AGENT_LLM_PROVIDER": ("llm_provider", str),
    "AGENT_LLM_EFFORT": ("llm_effort", str),
    "AGENT_LLM_MAX_OUTPUT_TOKENS": ("llm_max_output_tokens", _as_int),
    "AGENT_IMPACT_THRESHOLD": ("impact_threshold", _as_int),
    "AGENT_HIGH_IMPACT_CATEGORIES": ("high_impact_categories", _as_csv),
    "AGENT_BENCHMARK_INDEX": ("benchmark_index", str),
    "AGENT_CRON_SCHEDULE": ("cron_schedule", str),
    "AGENT_ACTIVE_HOURS_START": ("active_hours_start", str),
    "AGENT_ACTIVE_HOURS_END": ("active_hours_end", str),
    "AGENT_QUIET_HOURS_START": ("quiet_hours.start", str),
    "AGENT_QUIET_HOURS_END": ("quiet_hours.end", str),
    "AGENT_CHANNELS": ("channels", _as_channels),
    "AGENT_NEWS_SOURCES": ("news_sources", _as_csv),
    "AGENT_STATE_STORE_TYPE": ("state_store.type", str),
    "AGENT_STATE_STORE_PATH": ("state_store.path", str),
    "AGENT_ALREADY_ALERTED_TTL_HOURS": ("already_alerted_ttl_hours", _as_int),
    "AGENT_COST_MAX_INPUT_TOKENS_PER_RUN": ("cost.max_input_tokens_per_run", _as_int),
    "AGENT_COST_DAILY_COST_CAP_USD": ("cost.daily_cost_cap_usd", _as_float),
    "AGENT_NOTION_DATABASE_ID": ("notion.database_id", str),
    "AGENT_NOTION_TITLE_PROPERTY": ("notion.title_property", str),
    "AGENT_NOTION_INCLUDE_PROPERTY": ("notion.include_property", str),
    "AGENT_NOTION_GROUP_PROPERTY": ("notion.group_property", str),
    "AGENT_NOTION_TICKER_PATTERN": ("notion.ticker_pattern", str),
}


def _set_path(data: dict, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    cursor = data
    for part in parts[:-1]:
        existing = cursor.get(part)
        if not isinstance(existing, dict):
            existing = {}
            cursor[part] = existing
        cursor = existing
    cursor[parts[-1]] = value


class ConfigLoader:
    """Reads a YAML/JSON config file, applies env overrides, and validates it."""

    def __init__(self, env: Optional[dict[str, str]] = None) -> None:
        self._env = env if env is not None else os.environ

    # -- loading -----------------------------------------------------------

    def load(self, path: str) -> AgentConfig:
        raw = self._read_file(path)
        raw = self.apply_env_overrides(raw)
        return self._build(raw)

    def _read_file(self, path: str) -> dict:
        if not os.path.isfile(path):
            raise ConfigError(f"Config file not found: {path}")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            # yaml.safe_load parses JSON too, since JSON is a subset of YAML.
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ConfigError(f"Config file {path} is not parseable: {exc}") from exc
        except OSError as exc:
            raise ConfigError(f"Config file {path} could not be read: {exc}") from exc

        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ConfigError(f"Config file {path} must contain a mapping at the top level")
        return data

    def apply_env_overrides(self, raw: dict) -> dict:
        """Requirement 10.1 / Property 20 -- env always wins."""
        merged = json.loads(json.dumps(raw, default=str)) if raw else {}
        for env_name, (dotted, parser) in ENV_OVERRIDES.items():
            if env_name not in self._env:
                continue
            value = self._env[env_name]
            if not str(value).strip():
                # An unset GitHub Actions variable/secret still arrives as an
                # empty string; that must not clobber the config file value.
                continue
            try:
                _set_path(merged, dotted, parser(value))
            except (TypeError, ValueError) as exc:
                raise ConfigError(f"Environment variable {env_name}={value!r} is invalid: {exc}")
        return merged

    def _build(self, raw: dict) -> AgentConfig:
        watchlist = None
        if raw.get("watchlist") is not None:
            watchlist = [self._build_ticker_entry(item) for item in _as_list(raw["watchlist"])]

        notion = None
        notion_raw = raw.get("notion")
        if isinstance(notion_raw, dict):
            defaults = NotionConfig(database_id="")
            notion = NotionConfig(
                database_id=str(notion_raw.get("database_id") or ""),
                title_property=notion_raw.get("title_property") or defaults.title_property,
                include_property=notion_raw.get("include_property") or defaults.include_property,
                group_property=notion_raw.get("group_property") or None,
                ticker_pattern=notion_raw.get("ticker_pattern") or defaults.ticker_pattern,
            )

        quiet_hours = None
        quiet_raw = raw.get("quiet_hours")
        if isinstance(quiet_raw, dict) and (quiet_raw.get("start") or quiet_raw.get("end")):
            quiet_hours = QuietHoursConfig(
                start=str(quiet_raw.get("start", "")),
                end=str(quiet_raw.get("end", "")),
            )

        state_raw = raw.get("state_store") or {}
        state_store = StateStoreConfig(
            type=str(state_raw.get("type", "redis")).lower(),
            path=state_raw.get("path") or None,
        )

        cost_raw = raw.get("cost") or {}
        pricing_raw = cost_raw.get("model_pricing") or {}
        model_pricing: dict[str, ModelPricing] = {}
        for model, prices in pricing_raw.items():
            if not isinstance(prices, dict):
                continue
            try:
                model_pricing[model] = ModelPricing(
                    input_per_1m_usd=float(prices.get("input_per_1m_usd", 0.0)),
                    output_per_1m_usd=float(prices.get("output_per_1m_usd", 0.0)),
                )
            except (TypeError, ValueError):
                # Surfaced as a validation error rather than an exception here.
                continue
        cost = CostConfig(
            max_input_tokens_per_run=_coerce_int(cost_raw.get("max_input_tokens_per_run"), 100_000),
            daily_cost_cap_usd=_coerce_float(cost_raw.get("daily_cost_cap_usd"), 1.00),
            model_pricing=model_pricing,
        )

        channels = [
            ChannelConfig(type=str(item.get("type", "")).lower())
            if isinstance(item, dict)
            else ChannelConfig(type=str(item).lower())
            for item in _as_list(raw.get("channels"))
        ]

        defaults = AgentConfig()
        return AgentConfig(
            timezone=str(raw.get("timezone", defaults.timezone)),
            watchlist_source=str(raw.get("watchlist_source", defaults.watchlist_source)).lower(),
            watchlist=watchlist,
            notion=notion,
            lookback_window_hours=_coerce_int(raw.get("lookback_window_hours"), 24),
            price_window_hours=_coerce_int(raw.get("price_window_hours"), 2),
            llm_model=str(raw.get("llm_model", defaults.llm_model)),
            llm_provider=(str(raw["llm_provider"]).lower() if raw.get("llm_provider") else None),
            llm_effort=str(raw.get("llm_effort", defaults.llm_effort)).lower(),
            llm_max_output_tokens=_coerce_int(raw.get("llm_max_output_tokens"), 8000),
            impact_threshold=_coerce_int(raw.get("impact_threshold"), 6),
            high_impact_categories=[str(c) for c in _as_list(raw.get("high_impact_categories"))]
            or list(DEFAULT_CATEGORIES),
            benchmark_index=str(raw.get("benchmark_index", defaults.benchmark_index)),
            cron_schedule=(
                [str(c) for c in raw["cron_schedule"]]
                if isinstance(raw.get("cron_schedule"), list)
                else str(raw.get("cron_schedule", defaults.cron_schedule))
            ),
            active_hours_start=str(raw.get("active_hours_start", defaults.active_hours_start)),
            active_hours_end=str(raw.get("active_hours_end", defaults.active_hours_end)),
            quiet_hours=quiet_hours,
            channels=channels,
            news_sources=[str(s).lower() for s in _as_list(raw.get("news_sources"))]
            or ["yfinance", "google_news"],
            state_store=state_store,
            cost=cost,
            already_alerted_ttl_hours=_coerce_int(raw.get("already_alerted_ttl_hours"), 168),
        )

    @staticmethod
    def _build_ticker_entry(item: Any) -> TickerEntry:
        if isinstance(item, dict):
            return TickerEntry(
                symbol=str(item.get("symbol", "")).strip().upper(),
                group=item.get("group") or None,
            )
        return TickerEntry(symbol=str(item).strip().upper(), group=None)

    # -- validation --------------------------------------------------------

    def validate(self, config: AgentConfig, requires_watchlist: bool = True) -> list[str]:
        """Return every validation error (Requirement 10.4 -- report all, then exit).

        ``requires_watchlist=False`` for a backtest, which takes its tickers
        from the fixture and never reaches the watchlist source. Demanding a
        Notion database id to replay a canned file would make the one command
        that needs no credentials at all refuse to run without them.
        """
        errors: list[str] = []

        if config.watchlist_source not in SUPPORTED_WATCHLIST_SOURCES:
            errors.append(
                f"watchlist_source: must be one of {sorted(SUPPORTED_WATCHLIST_SOURCES)}, "
                f"got {config.watchlist_source!r}"
            )
        elif not requires_watchlist:
            pass
        elif config.watchlist_source == "static":
            errors.extend(self._validate_static_watchlist(config))
        elif config.watchlist_source == "notion":
            errors.extend(self._validate_notion(config))

        for key, value in (
            ("lookback_window_hours", config.lookback_window_hours),
            ("price_window_hours", config.price_window_hours),
            ("already_alerted_ttl_hours", config.already_alerted_ttl_hours),
        ):
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                errors.append(f"{key}: must be a positive integer, got {value!r}")

        for key, value in (
            ("active_hours_start", config.active_hours_start),
            ("active_hours_end", config.active_hours_end),
        ):
            if not HHMM_PATTERN.match(str(value)):
                errors.append(f"{key}: must be HH:MM 24-hour format, got {value!r}")

        if config.quiet_hours is not None:
            for key, value in (
                ("quiet_hours.start", config.quiet_hours.start),
                ("quiet_hours.end", config.quiet_hours.end),
            ):
                if not HHMM_PATTERN.match(str(value)):
                    errors.append(f"{key}: must be HH:MM 24-hour format, got {value!r}")

        try:
            ZoneInfo(config.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            errors.append(f"timezone: {config.timezone!r} is not a valid IANA timezone identifier")

        # Accepts one expression or a list, so it can mirror a workflow that
        # fires several times a day rather than misreporting one of them.
        crons = config.cron_schedule if isinstance(config.cron_schedule, list) else [config.cron_schedule]
        if not crons:
            errors.append("cron_schedule: must contain at least one cron expression")
        for entry in crons:
            cron = str(entry).strip()
            if len(cron.split()) != 5 or not croniter.is_valid(cron):
                errors.append(
                    f"cron_schedule: must be a valid 5-field cron expression, got {cron!r}"
                )

        for channel in config.channels:
            if channel.type not in SUPPORTED_CHANNEL_TYPES:
                errors.append(
                    f"channels: unsupported type {channel.type!r}; "
                    f"must be one of {sorted(SUPPORTED_CHANNEL_TYPES)}"
                )

        for source in config.news_sources:
            if source not in SUPPORTED_NEWS_SOURCES:
                errors.append(
                    f"news_sources: unsupported source {source!r}; "
                    f"must be one of {sorted(SUPPORTED_NEWS_SOURCES)}"
                )
        if not config.news_sources:
            errors.append("news_sources: at least one news source must be configured")

        if (
            not isinstance(config.impact_threshold, int)
            or isinstance(config.impact_threshold, bool)
            or not 0 <= config.impact_threshold <= 10
        ):
            errors.append(
                f"impact_threshold: must be an integer 0-10, got {config.impact_threshold!r}"
            )

        if not config.high_impact_categories:
            errors.append("high_impact_categories: must list at least one category")

        if config.llm_provider is not None and config.llm_provider not in SUPPORTED_LLM_PROVIDERS:
            errors.append(
                f"llm_provider: must be one of {sorted(SUPPORTED_LLM_PROVIDERS)} (or unset to "
                f"infer from llm_model), got {config.llm_provider!r}"
            )
        if config.llm_effort not in SUPPORTED_LLM_EFFORTS:
            errors.append(
                f"llm_effort: must be one of {sorted(SUPPORTED_LLM_EFFORTS)}, "
                f"got {config.llm_effort!r}"
            )
        if (
            not isinstance(config.llm_max_output_tokens, int)
            or isinstance(config.llm_max_output_tokens, bool)
            or config.llm_max_output_tokens <= 0
        ):
            errors.append(
                f"llm_max_output_tokens: must be a positive integer, "
                f"got {config.llm_max_output_tokens!r}"
            )

        if not is_valid_ticker(str(config.benchmark_index).upper()):
            errors.append(f"benchmark_index: {config.benchmark_index!r} is not a valid ticker")

        cap = config.cost.daily_cost_cap_usd
        if not isinstance(cap, (int, float)) or isinstance(cap, bool) or cap <= 0:
            errors.append(f"cost.daily_cost_cap_usd: must be a positive number, got {cap!r}")
        max_tokens = config.cost.max_input_tokens_per_run
        if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
            errors.append(
                f"cost.max_input_tokens_per_run: must be a positive integer, "
                f"got {config.cost.max_input_tokens_per_run!r}"
            )

        if config.state_store.type not in SUPPORTED_STATE_STORES:
            errors.append(
                f"state_store.type: must be one of {sorted(SUPPORTED_STATE_STORES)}, "
                f"got {config.state_store.type!r}"
            )
        elif config.state_store.type == "sqlite" and not config.state_store.path:
            errors.append("state_store.path: required when state_store.type is 'sqlite'")

        return errors

    def _validate_static_watchlist(self, config: AgentConfig) -> list[str]:
        errors: list[str] = []
        if not config.watchlist:
            errors.append(
                "watchlist: required (and non-empty) when watchlist_source is 'static'"
            )
            return errors

        valid, rejected = validate_and_dedupe(config.watchlist)
        for symbol in rejected:
            errors.append(
                f"watchlist: {symbol!r} is not a valid ticker symbol "
                "(1-10 uppercase alphanumerics, optional .SUFFIX)"
            )
        if not MIN_WATCHLIST_SIZE <= len(valid) <= MAX_WATCHLIST_SIZE:
            errors.append(
                f"watchlist: must contain between {MIN_WATCHLIST_SIZE} and "
                f"{MAX_WATCHLIST_SIZE} unique tickers, got {len(valid)}"
            )
        return errors

    def _validate_notion(self, config: AgentConfig) -> list[str]:
        errors: list[str] = []
        if config.notion is None or not config.notion.database_id:
            errors.append("notion.database_id: required when watchlist_source is 'notion'")
            return errors
        try:
            re.compile(config.notion.ticker_pattern)
        except re.error as exc:
            errors.append(f"notion.ticker_pattern: not a valid regex ({exc})")
        if not config.notion.include_property:
            errors.append("notion.include_property: must name a Notion checkbox property")
        if not config.notion.title_property:
            errors.append("notion.title_property: must name the Notion title property")
        return errors


def _as_list(value: Any) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _coerce_int(value: Any, default: int) -> Any:
    if value is None:
        return default
    if isinstance(value, bool):
        return value  # let validation reject it
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return value  # let validation reject it


def _coerce_float(value: Any, default: float) -> Any:
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return value  # let validation reject it
