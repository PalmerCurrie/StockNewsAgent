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

from .models import (
    AgentConfig,
    ChannelConfig,
    CostConfig,
    ModelPricing,
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
SUPPORTED_STATE_STORES = {"sqlite", "redis", "memory"}
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
    "AGENT_WATCHLIST_FILE": ("watchlist_file", str),
    "AGENT_LOOKBACK_WINDOW_HOURS": ("lookback_window_hours", _as_int),
    "AGENT_PRICE_WINDOW_HOURS": ("price_window_hours", _as_int),
    "AGENT_LLM_MODEL": ("llm_model", str),
    "AGENT_LLM_PROVIDER": ("llm_provider", str),
    "AGENT_LLM_EFFORT": ("llm_effort", str),
    "AGENT_LLM_MAX_OUTPUT_TOKENS": ("llm_max_output_tokens", _as_int),
    "AGENT_IMPACT_THRESHOLD": ("impact_threshold", _as_int),
    "AGENT_HIGH_IMPACT_CATEGORIES": ("high_impact_categories", _as_csv),
    "AGENT_BENCHMARK_INDEX": ("benchmark_index", str),
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
        #: Problems reading the watchlist file. Held rather than raised so that
        #: a --backtest, which never uses the watchlist, is not blocked by one.
        #: validate() reports them only when the run actually needs tickers.
        self._watchlist_errors: list[str] = []

    # -- loading -----------------------------------------------------------

    def load(self, path: str) -> AgentConfig:
        raw = self._read_file(path)
        raw = self.apply_env_overrides(raw)
        config = self._build(raw)
        self._watchlist_errors = []
        config.watchlist = self._read_watchlist(config.watchlist_file, relative_to=path)
        return config

    def _read_watchlist(self, path: str, relative_to: str) -> Optional[list[TickerEntry]]:
        """Load the ticker list from its own file.

        Resolved relative to the config file rather than the process working
        directory, so `--config /elsewhere/config.yaml` finds the watchlist
        sitting beside it rather than one in whatever directory you happened to
        run from.
        """
        resolved = path
        if not os.path.isabs(path):
            resolved = os.path.join(os.path.dirname(os.path.abspath(relative_to)), path)

        if not os.path.isfile(resolved):
            self._watchlist_errors.append(
                f"watchlist_file: {path!r} not found (looked in {resolved!r}). "
                "Copy watchlist.example.yaml to watchlist.yaml and list your tickers."
            )
            return None
        try:
            with open(resolved, "r", encoding="utf-8") as handle:
                data = yaml.safe_load(handle.read())
        except yaml.YAMLError as exc:
            self._watchlist_errors.append(f"watchlist_file: {path!r} is not parseable: {exc}")
            return None
        except OSError as exc:
            self._watchlist_errors.append(f"watchlist_file: {path!r} could not be read: {exc}")
            return None

        # Accept a bare list (`- AAPL`) or a mapping with a `watchlist:` key, so
        # that a file written either way does what it looks like it should.
        if isinstance(data, dict):
            data = data.get("watchlist")
        if data is None:
            return []
        if not isinstance(data, list):
            self._watchlist_errors.append(
                f"watchlist_file: {path!r} must contain a list of tickers"
            )
            return None
        return [self._build_ticker_entry(item) for item in data]

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
        quiet_hours = None
        quiet_raw = raw.get("quiet_hours")
        if isinstance(quiet_raw, dict) and (quiet_raw.get("start") or quiet_raw.get("end")):
            quiet_hours = QuietHoursConfig(
                start=str(quiet_raw.get("start", "")),
                end=str(quiet_raw.get("end", "")),
            )

        state_defaults = StateStoreConfig()
        state_raw = raw.get("state_store") or {}
        state_type = str(state_raw.get("type", state_defaults.type)).lower()
        state_store = StateStoreConfig(
            type=state_type,
            # Only default the path for sqlite; a redis or memory store with a
            # stray path would be confusing rather than helpful.
            path=state_raw.get("path")
            or (state_defaults.path if state_type == "sqlite" else None),
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
            watchlist_file=str(raw.get("watchlist_file", defaults.watchlist_file)),
            watchlist=None,  # filled in by load(), from watchlist_file
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
        from the fixture and never reads the watchlist file. Demanding a
        watchlist to replay a canned file would make the one command that needs
        no setup at all refuse to run without it.
        """
        errors: list[str] = []

        if requires_watchlist:
            errors.extend(self._watchlist_errors)
            if not self._watchlist_errors:
                errors.extend(self._validate_watchlist(config))

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

    def _validate_watchlist(self, config: AgentConfig) -> list[str]:
        errors: list[str] = []
        if not config.watchlist:
            errors.append(
                f"watchlist_file: {config.watchlist_file!r} is empty -- list at least "
                f"{MIN_WATCHLIST_SIZE} ticker in it"
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
