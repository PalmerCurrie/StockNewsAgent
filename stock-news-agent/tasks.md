# Implementation Plan: Stock News Agent

## Overview

Implement a Python-based scheduled pipeline that resolves a watchlist (from a static config file or a live Notion database query), fetches price data and news stories, processes them through an LLM with structured JSON output, deduplicates against a persistent State_Store, and dispatches formatted alerts to configured notification channels. The pipeline runs as a short-lived process on an ephemeral GitHub Actions runner, triggered by a `schedule` cron expression, with a State_Store-backed lock preventing overlapping runs and an `already_alerted` set preventing duplicate notifications across runs. The default State_Store backend is Redis (e.g. Upstash's free tier), since GitHub Actions runners have no persistent local disk between invocations.

## Tasks

- [x] 1. Set up project structure, data models, and core interfaces
  - Create the `agent/` package directory with `__init__.py`
  - Create `agent/models.py` defining all dataclasses: `Story`, `PriceData`, `OHLCVBar`, `PriceMovementResult`, `Event`, `Alert`, `QuietHoursConfig`, `ChannelConfig`, `DeliveryResult`, `RunSummary`, `AgentConfig`, `TickerEntry`, `NotionConfig`, `StateStoreConfig`, `CostConfig`, `ModelPricing`, `RunMode` (enum), `LockToken`
  - Create `tests/unit/`, `tests/property/`, `tests/snapshot/`, `tests/integration/`, `tests/fixtures/news/`, `tests/fixtures/backtest/` directories with `__init__.py` files
  - Create `requirements.txt` with pinned dependencies: `yfinance`, `openai`, `feedparser`, `requests`, `beautifulsoup4`, `hypothesis`, `pytest`, `pyyaml`, `structlog`, `croniter`, `redis`
  - Standard library: use `zoneinfo` for timezone handling (no `pytz` needed on Python 3.9+); use `sqlite3` for the local-dev-only `SQLiteStateStore`
  - Note: `redis` is a required (not optional) dependency — it is the default State_Store backend for the GitHub Actions deployment, not an alternative. The Notion API is called via plain `requests` (no dedicated Notion SDK needed) against `https://api.notion.com/v1/...`
  - _Requirements: 1.1, 3.1, 4.5, 6.1, 11.1_

  - [x] 1.1 Create data model file with all dataclasses
    - Implement all dataclasses in `agent/models.py` as specified in the design
    - Include `Event.identity_key`, `Event.impact_score`, `Event.category`, `Event.market_reaction`, `Event.price_movement_pending`, `Event.group`
    - Include `Alert.run_timestamp_utc`, `Alert.run_timestamp_local`, `Alert.earnings_upcoming`
    - Include `RunSummary.mode`, `RunSummary.events_dispatched`, `RunSummary.events_skipped_already_alerted`, `RunSummary.llm_cost_usd_this_run`, `RunSummary.daily_cost_usd_running_total`
    - Include `AgentConfig.watchlist_source`, `AgentConfig.notion`, `NotionConfig.database_id/title_property/include_property/group_property/ticker_pattern`
    - _Requirements: 3.1, 4.5, 6.1, 11.3_

- [x] 2. Implement ConfigLoader
  - [x] 2.1 Implement `agent/config_loader.py` with `ConfigLoader` class
    - Implement `load(path)` to read YAML or JSON config file and merge with environment variable overrides using `AGENT_<KEY>` convention
    - Implement `validate(config)` to enforce all validation rules: `watchlist_source` in `{static, notion}` with the corresponding section present (`watchlist` for static, `notion.database_id` for notion), watchlist length 1–50 (after merging groups, when statically resolvable), positive integer windows, HH:MM quiet hours, valid IANA `timezone` via `zoneinfo.ZoneInfo(...)`, single valid 5-field cron expression via `croniter.is_valid`, supported channel types, `impact_threshold` 0–10, positive `daily_cost_cap_usd`, supported `state_store.type`
    - Support `watchlist:` entries either as bare strings (no group) or as `{symbol, group}` mappings (only relevant when `watchlist_source: static`)
    - Log all validation errors before exiting; exit code 1 on any validation failure
    - Sensitive values (API keys, tokens, `STATE_STORE_REDIS_URL`, `NOTION_API_TOKEN`) read exclusively from environment variables
    - _Requirements: 1.1, 1.2, 1.6, 10.1, 10.2, 10.3, 10.4, 10.5_

  - [ ]* 2.2 Write property test for environment variable precedence
    - **Property 20: Environment variables always take precedence over config file values**
    - **Validates: Requirements 10.1**
    - File: `tests/property/test_config_validation.py`

  - [ ]* 2.3 Write property test for config validation completeness
    - **Property 21: Config validation reports all errors before any data fetching**
    - **Validates: Requirements 10.4**
    - File: `tests/property/test_config_validation.py`

  - [ ]* 2.4 Write property test for config field validation rules
    - **Property 22: Config validation accepts valid values and rejects invalid ones for all fields**
    - **Validates: Requirements 10.5**
    - File: `tests/property/test_config_validation.py`

  - [ ]* 2.5 Write unit tests for ConfigLoader
    - Test loading valid YAML, valid JSON, missing file, malformed YAML, missing `watchlist` key (static mode), missing `notion.database_id` (notion mode)
    - Test environment variable override for each config key
    - File: `tests/unit/test_config_loader.py`
    - _Requirements: 1.1, 1.2, 10.1, 10.2, 10.4, 10.5_

- [x] 3. Implement Logger
  - [x] 3.1 Implement `agent/logger.py` with `Logger` class
    - Configure `structlog` with a JSON renderer to stdout, ISO timestamps, and `event` keyed messages
    - Bind `run_id` (UUID4) and `mode` at the top of the run so they appear on every entry
    - Implement `info()`, `warning()`, `error()` methods with `component` and `message` parameters
    - Implement `llm_call(model, input_tokens, output_tokens, cost_usd, severity)` for LLM-call entries
    - Implement `write_run_summary(summary: RunSummary)` to emit the run-level log entry
    - _Requirements: 9.1, 9.2, 9.3, 9.4, 9.5_

  - [ ]* 3.2 Write property test for run-level log entry fields
    - **Property 18: Run-level log entries always contain all required fields**
    - **Validates: Requirements 9.1**
    - File: `tests/property/test_logging.py`

  - [ ]* 3.3 Write property test for component error log entry fields
    - **Property 19: Component error log entries always contain required fields**
    - **Validates: Requirements 9.2**
    - File: `tests/property/test_logging.py`

  - [ ]* 3.4 Write unit tests for Logger
    - Test that each log method emits valid JSON to stdout
    - Test that `run_id` is present in every entry
    - Test `write_run_summary` output structure
    - File: `tests/unit/test_logger.py`
    - _Requirements: 9.1, 9.2, 9.3, 9.4_

- [x] 4. Implement StateStore (P0)
  - [x] 4.1 Implement `agent/state_store/base.py` with the `StateStore` interface from the design
    - Methods: `get`, `set`, `delete`, `add_to_set`, `set_contains`, `increment`, `acquire_lock`, `release_lock`
    - Define `LockToken` dataclass
    - _Requirements: 11.1, 11.2_

  - [x] 4.2 Implement `agent/state_store/redis_store.py` with `RedisStateStore` (default backend)
    - Map to native Redis commands (`SETEX`, `SADD` + `EXPIRE`, `INCRBYFLOAT`, `SET ... NX EX`); connection string from `STATE_STORE_REDIS_URL`
    - This is the backend used by the GitHub Actions deployment (default `state_store.type: redis`), since runners have no persistent disk between runs
    - _Requirements: 11.1, 11.7_

  - [x] 4.3 Implement `agent/state_store/sqlite_store.py` with `SQLiteStateStore` (local/dev only)
    - Tables: `kv(key, value, expires_at)`, `set_members(key, member, expires_at)`, `counters(key, value)`
    - Use `WITH IMMEDIATE` transactions for atomic lock acquisition
    - Background-on-read sweep of expired entries (TTL semantics on reads)
    - `acquire_lock` inserts a row with `(key, token, expires_at)` if no live entry exists; returns `LockToken` or `None`
    - Intended for local development/testing; not used in the GitHub Actions deployment (no persistent volume there)
    - _Requirements: 11.1, 11.2, 11.5_

  - [x] 4.4 Implement `agent/state_store/memory_store.py` with `MemoryStateStore` (test-only)
    - In-process dict; agent refuses to start in `live` mode with this backend
    - _Requirements: 11.1_

  - [x] 4.5 Implement factory `agent/state_store/__init__.py` that returns the right backend per `state_store.type`
    - Verify reachability at startup; log error and exit 1 if unreachable
    - _Requirements: 11.6_

  - [ ]* 4.6 Write property test for state store lock semantics
    - **Property 25: StateStore acquire_lock prevents overlapping runs**
    - **Validates: Requirements 2.4, 11.2**
    - File: `tests/property/test_state_store.py`

  - [ ]* 4.7 Write unit tests for each StateStore backend
    - kv with TTL, set add/contains, counters, lock acquire/release races
    - File: `tests/unit/test_state_store.py`

- [x] 5. Implement TimeGate and CostGuard (P0/P1)
  - [x] 5.1 Implement `agent/time_gate.py` with `TimeGate`
    - Pure functions `in_active_hours(now)` and `in_quiet_hours(now)` using `zoneinfo.ZoneInfo(config.timezone)`
    - Handle midnight-spanning quiet-hours ranges
    - _Requirements: 2.2, 8.1_

  - [x] 5.2 Implement `agent/cost_guard.py` with `CostGuard`
    - `project_cost(tokens_in, tokens_out_estimate, model)` using `CostConfig.model_pricing`
    - `would_exceed_cap(projected_cost)` consults `daily_cost_ledger[UTC_date]` via StateStore
    - `record(actual_cost)` atomically increments the ledger
    - Warn at startup if configured model is not in pricing table (cap effectively disabled)
    - _Requirements: 12.1, 12.2, 12.3, 12.4_

  - [ ]* 5.3 Write property test for daily cost cap
    - **Property 28: Daily cost cap is never exceeded**
    - **Validates: Requirements 12.2**
    - File: `tests/property/test_cost_guard.py`

  - [ ]* 5.4 Write property test for per-run token cap
    - **Property 29: Per-run input token cap is never exceeded**
    - **Validates: Requirements 12.1**
    - File: `tests/property/test_cost_guard.py`

  - [ ]* 5.5 Write property test for active/quiet hours gating
    - **Property 3: Active-hours suppression is correct for all timestamps**
    - **Property 16: Quiet-hours suppression is correct for all timestamps and ranges (including midnight-spanning)**
    - File: `tests/property/test_time_gate.py`

  - [ ]* 5.6 Write unit tests for TimeGate and CostGuard
    - File: `tests/unit/test_time_gate.py`, `tests/unit/test_cost_guard.py`

- [x] 6. Implement WatchlistSource (static file + Notion sync)
  - [x] 6.1 Implement `agent/watchlist_source/base.py` with the `WatchlistSource` interface
    - Single method: `fetch() -> list[TickerEntry]`
    - _Requirements: 1.1_

  - [x] 6.2 Implement `agent/watchlist_source/static_source.py` with `StaticWatchlistSource`
    - `fetch()` returns `config.watchlist` as-is (pure, no I/O)
    - _Requirements: 1.2_

  - [x] 6.3 Implement `agent/watchlist_source/notion_source.py` with `NotionWatchlistSource`
    - `fetch()` queries the Notion database at `config.notion.database_id` via `requests` against the Notion API (`NOTION_API_TOKEN` bearer auth, `Notion-Version` header pinned to a specific date), paginating through `has_more`/`next_cursor`
    - Filter to pages where `config.notion.include_property` (checkbox) is `true` — apply as a server-side filter in the query payload where possible, else filter client-side
    - For each included page, extract the ticker by applying `re.search(config.notion.ticker_pattern, title_property_value)`; on no match, log a `warning` with the Notion page URL and `component=NotionWatchlistSource`, and exclude that page (do not fail the run)
    - When `config.notion.group_property` is set, read that property's value (select/status/text) as `TickerEntry.group`
    - On success: serialize the resulting `list[TickerEntry]` and write it to `StateStore` under key `watchlist_cache` (no TTL)
    - On failure (timeout, non-2xx, network error): log a `warning`, read `watchlist_cache` from `StateStore` and return it; if no cache exists, raise a distinguishable exception so the Agent can log an `error` and exit 0 without dispatching
    - _Requirements: 1.3, 1.4, 1.5, 1.6_

  - [x] 6.4 Implement factory `agent/watchlist_source/__init__.py` that returns the right source per `watchlist_source`
    - _Requirements: 1.1_

  - [ ]* 6.5 Write property test for Notion filtering and ticker extraction
    - **Property 34: Notion watchlist sync includes exactly the checked, parseable rows**
    - **Validates: Requirements 1.3, 1.4**
    - File: `tests/property/test_watchlist_source.py`

  - [ ]* 6.6 Write property test for cache fallback on Notion failure
    - **Property 35: Notion fetch failure falls back to the cached watchlist, never silently to an empty watchlist**
    - **Validates: Requirements 1.6**
    - File: `tests/property/test_watchlist_source.py`

  - [ ]* 6.7 Write unit tests for WatchlistSource
    - Mock Notion HTTP responses: pagination, missing checkbox property, unmatched title, API error with cache present, API error with no cache
    - Test `StaticWatchlistSource` passthrough
    - File: `tests/unit/test_watchlist_source.py`
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6_

- [ ] 7. Checkpoint — Ensure project structure, models, config, logger, state store, time gate, cost guard, and watchlist source are correct
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Implement IngestionModule
  - [x] 8.1 Implement `agent/ingestion_module.py` with `IngestionModule` class
    - Implement `fetch_prices(tickers)` using `yfinance.download()` with `interval="5m"` for intraday and `interval="1d"` for trailing 5 days
    - Implement `fetch_earnings_calendar(tickers)` via `yf.Ticker(t).calendar`; per-ticker failures map to `None` with a warning
    - Log warnings for unrecognized tickers and exclude them from the result dict
    - Log warnings for missing OHLCV fields; use available fields for that interval
    - Log info entry when falling back to daily close outside market hours
    - Exit (log error, return empty dict) when all tickers are invalid
    - _Requirements: 1.7, 1.8, 3.1, 3.2, 3.3, 3.6, 3.7_

  - [x] 8.2 Implement `calculate_price_movement` as a pure function returning `PriceMovementResult`
    - Implement the anchoring table for regular-hours, pre-market, after-hours, weekend/holiday, and next-session-not-yet-open cases (returns `pending: true` for the last)
    - Use last available close price when window extends beyond available data; set `truncated: true` and log
    - _Requirements: 3.4, 3.5_

  - [ ]* 8.3 Write property test for price movement calculation including non-market-hours rules
    - **Property 4: Price movement calculation is correct for any price series**
    - **Validates: Requirements 3.4, 3.5**
    - File: `tests/property/test_price_movement.py`

  - [ ]* 8.4 Write unit tests for IngestionModule
    - Mock yfinance; test unrecognized ticker warning, empty data exclusion, after-hours fallback, earnings calendar fetch
    - Test `calculate_price_movement` for each anchoring case and truncated window
    - File: `tests/unit/test_ingestion_module.py`
    - _Requirements: 1.7, 1.8, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 9. Implement ticker validation and watchlist deduplication
  - [x] 9.1 Implement ticker format validation and deduplication in `agent/config_loader.py` (or a `agent/ticker_utils.py` helper)
    - Validate ticker format against `[A-Z0-9]{1,10}(\.[A-Z]{1,4})?`
    - Silently deduplicate duplicate ticker symbols, retaining one entry; preserve group label from first occurrence
    - Applies uniformly to the `list[TickerEntry]` returned by either `WatchlistSource` implementation
    - _Requirements: 1.7, 1.10_

  - [ ]* 9.2 Write property test for ticker deduplication idempotency
    - **Property 1: Ticker deduplication is idempotent**
    - **Validates: Requirements 1.7**
    - File: `tests/property/test_ticker_validation.py`

  - [ ]* 9.3 Write property test for ticker format validation
    - **Property 2: Ticker format validation accepts valid symbols and rejects invalid ones**
    - **Validates: Requirements 1.7**
    - File: `tests/property/test_ticker_validation.py`

- [x] 10. Implement NewsFetcher
  - [x] 10.1 Implement `agent/news_fetcher.py` with `NewsFetcher` class and `NewsSourceAdapter` interface
    - Implement `YFinanceNewsAdapter` using `yf.Ticker(symbol).news`
    - Implement `GoogleNewsRSSAdapter` using `feedparser` against `https://news.google.com/rss/search?q={ticker}+stock`
    - Implement `FinvizNewsAdapter` using `requests` + `BeautifulSoup`
    - Implement `YahooFinanceRSSAdapter` using `feedparser`
    - Each adapter enforces a 30-second timeout; failures are caught, logged as warnings, and skipped
    - Associate each story with ticker, publication timestamp, and source URL (fall back to source name if no URL)
    - When ≥10% of fetched items from a source fail to parse, log a structured warning with `adapter_name`, `parse_failure_count`, and a 500-char-truncated sample
    - _Requirements: 4.1, 4.2, 4.4, 4.5, 4.6, 14.3_

  - [x] 10.2 Implement story filtering and deduplication in `NewsFetcher.fetch_stories()`
    - Filter stories to those within the `Lookback_Window`
    - Deduplicate by exact URL match, then by `(ticker, headline, published_at)` composite key
    - _Requirements: 4.3, 4.7_

  - [ ]* 10.3 Write property test for lookback window filtering
    - **Property 5: News stories are filtered to the lookback window**
    - **Validates: Requirements 4.3**
    - File: `tests/property/test_news_filtering.py`

  - [ ]* 10.4 Write property test for required story fields
    - **Property 6: Every story has required association fields**
    - **Validates: Requirements 4.5**
    - File: `tests/property/test_news_filtering.py`

  - [ ]* 10.5 Write property test for story deduplication
    - **Property 7: Story deduplication removes all duplicate URLs and composite-key matches**
    - **Validates: Requirements 4.7**
    - File: `tests/property/test_news_filtering.py`

  - [ ]* 10.6 Write unit tests for NewsFetcher
    - Mock HTTP calls; test timeout handling, empty response, missing URL fallback, all-sources-fail path
    - File: `tests/unit/test_news_fetcher.py`
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7_

  - [ ]* 10.7 Write snapshot tests for each adapter
    - Save sample payloads under `tests/fixtures/news/{adapter}_sample.json` (or `.html` for Finviz)
    - One test per adapter that parses the fixture and asserts the resulting `Story` field shape; fails loudly on drift
    - **Validates: Requirements 14.1, 14.2**
    - File: `tests/snapshot/test_{adapter}_adapter.py`

- [ ] 11. Checkpoint — Ensure ingestion and news fetching tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 12. Implement LLMProcessor with structured output
  - [x] 12.1 Implement `agent/llm_processor.py` with `LLMProcessor` class
    - One prompt per ticker; instruct the model to group same-event stories, assign `category` and `impact_score`, produce ≤150-word summaries with at least one source URL, populate `market_reaction` against the benchmark index when price data is available
    - Use OpenAI `response_format={"type": "json_schema", ...}` (or function-calling fallback) per the Event schema in design.md
    - Implement cross-source deduplication: retain story with greatest word count, merge unique source URLs
    - Filter Events by `impact_score >= impact_threshold` AND `category` in `high_impact_categories`
    - Produce "no significant events" summary (≤50 words) when no events pass the filter
    - Note absence of price data in the summary when `PriceData` is missing or `price_movement_pending`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.9_

  - [x] 12.2 Implement LLM retry, schema-validation retry, and cost logging
    - Single retry after 5s on timeout or transient API error; on double failure, skip the **ticker** (not the run)
    - Single retry on JSON-schema validation failure, embedding the validator error in the prompt; on second failure log `error` and skip the ticker
    - Before each call: consult `CostGuard.project_cost` and `would_exceed_cap`; skip remaining calls and log warning if the cap or token budget is hit
    - After each call: `CostGuard.record(actual_cost)`; emit `Logger.llm_call(model, input_tokens, output_tokens, estimated_cost_usd, severity)`
    - _Requirements: 5.7, 5.8, 5.10, 9.3, 12.1, 12.2_

  - [ ]* 12.3 Write property test for LLM cross-source deduplication
    - **Property 8**
    - **Validates: Requirements 5.1**
    - File: `tests/property/test_llm_processing.py`

  - [ ]* 12.4 Write property test for event summary word limits
    - **Property 9**
    - **Validates: Requirements 5.4, 5.6**
    - File: `tests/property/test_llm_processing.py`

  - [ ]* 12.5 Write property test for LLM call log entry fields
    - **Property 10**
    - **Validates: Requirements 5.7, 9.3**
    - File: `tests/property/test_llm_processing.py`

  - [ ]* 12.6 Write property test for JSON-schema validation retry
    - **Property 26: LLM responses validate against the JSON schema or trigger one validation retry**
    - **Validates: Requirements 5.2, 5.10**
    - File: `tests/property/test_llm_processing.py`

  - [ ]* 12.7 Write property test for impact threshold filtering
    - **Property 27: Impact threshold filtering is correct**
    - **Validates: Requirements 5.3**
    - File: `tests/property/test_llm_processing.py`

  - [ ]* 12.8 Write unit tests for LLMProcessor
    - Mock OpenAI API; test retry logic (first failure + retry success, double failure + skip ticker), schema-retry, structured-output parsing, no-events path, market-reaction with vs. without benchmark data
    - File: `tests/unit/test_llm_processor.py`
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.7, 5.8, 5.9, 5.10_

- [x] 13. Implement AlertBuilder and event sorting
  - [x] 13.1 Implement `agent/alert_builder.py` with `AlertBuilder`
    - Build `Alert` with `run_id`, `run_timestamp_utc`, `run_timestamp_local`, `tickers_monitored`, `events`, `earnings_upcoming`
    - Sort events by descending `impact_score`, then descending `|price_movement|`, then ascending ticker
    - Group events by ticker; sub-group by group label when configured
    - Include `earnings_upcoming` for tickers with an earnings date within 7 calendar days
    - Implement `merge(current, suppressed)` deduplicating Events by `identity_key`
    - _Requirements: 6.1, 6.2, 6.3, 6.4, 6.5_

  - [ ]* 13.2 Write property test for alert required fields
    - **Property 11**; **Validates: Requirements 6.1**
    - File: `tests/property/test_alert_format.py`

  - [ ]* 13.3 Write property test for event sort order
    - **Property 12: Events are sorted by descending impact then absolute price movement, with alphabetical tiebreaking**
    - **Validates: Requirements 6.2**
    - File: `tests/property/test_alert_format.py`

  - [ ]* 13.4 Write property test for alert header fields and earnings window
    - **Property 13** and **Property 33: Earnings calendar surfacing window**
    - **Validates: Requirements 6.4, 6.5, 3.7**
    - File: `tests/property/test_alert_format.py`

- [x] 14. Implement Notifier with retry, per-channel limits, and test-channels
  - [x] 14.1 Implement `agent/notifier.py` with `Notifier` class and channel adapters
    - Implement `TelegramAdapter`: HTTP POST with `parse_mode=MarkdownV2`; multi-message split at event boundaries when body would exceed 4096 chars
    - Implement `DiscordAdapter`: webhook with plain `content` ≤2000 chars, falling back to embeds (≤6000 chars total) for longer alerts
    - Implement `EmailAdapter`: SMTP via `smtplib`; both plain-text and HTML alternatives; unbounded body length
    - Format markdown for Telegram/Discord (bold headers, bullets, inline links); plain text for email
    - Single retry with 2s backoff on transient errors (timeout, 5xx, 429); no retry on non-transient 4xx
    - Log channel failures with channel name and error details; continue to remaining channels
    - Implement `test_channels(channels)` sending a canned `"Stock News Agent: channel test, <ISO timestamp>"`
    - Log error and exit when zero channels configured
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5, 7.6, 7.7_

  - [ ]* 14.2 Write property test for dispatch to all channels
    - **Property 14**; **Validates: Requirements 7.3**
    - File: `tests/property/test_notifier.py`

  - [ ]* 14.3 Write property test for per-channel character limits
    - **Property 31: Per-channel character limits are enforced per-adapter**
    - **Validates: Requirements 7.5**
    - File: `tests/property/test_notifier.py`

  - [ ]* 14.4 Write property test for transient retry semantics
    - **Property 30: Notifier retries transient errors exactly once**
    - **Validates: Requirements 7.4**
    - File: `tests/property/test_notifier.py`

  - [ ]* 14.5 Write unit tests for Notifier
    - Mock HTTP/SMTP; test markdown vs plain text, channel failure isolation, retry-once-on-5xx, no-retry-on-400, zero-channels error, multi-message split for Telegram, embed fallback for Discord
    - File: `tests/unit/test_notifier.py`
    - _Requirements: 7.1, 7.3, 7.4, 7.5, 7.6, 7.7_

- [x] 15. Implement DedupFilter (cross-run alert deduplication)
  - [x] 15.1 Implement `agent/dedup_filter.py` with `DedupFilter`
    - Compute `identity_key` per Event: `sha256(normalized_url)` if URL present, else `sha256(ticker + lowercased_headline + ISO8601_date(published_at))`
    - `filter(events)` consults `StateStore.set_contains("already_alerted", key)` and drops matches (logged at `info`)
    - `record_dispatched(events)` adds each dispatched event's key to `already_alerted` with `already_alerted_ttl_hours` TTL
    - _Requirements: 11.3, 11.4, 11.5_

  - [ ]* 15.2 Write property test for cross-run dedup correctness
    - **Property 23: Cross-run deduplication never dispatches the same Event twice**
    - **Validates: Requirements 11.4, 11.5**
    - File: `tests/property/test_dedup.py`

  - [ ]* 15.3 Write property test for identity-key determinism
    - **Property 24: Event identity key is deterministic and source-agnostic**
    - **Validates: Requirements 11.3**
    - File: `tests/property/test_dedup.py`

  - [ ]* 15.4 Write unit tests for DedupFilter
    - In-memory StateStore; verify filter then record_dispatched then filter idempotency
    - File: `tests/unit/test_dedup_filter.py`

- [x] 16. Implement Operational Modes (dry-run, test-channels, backtest)
  - [x] 16.1 Implement `agent/modes.py` with `RunMode` enum and mode-dispatch helpers
    - `--dry-run` runs the full pipeline through LLM and prints the would-be Alert JSON; skips Notifier; skips `DedupFilter.record_dispatched`
    - `--dry-run --no-llm` substitutes a deterministic canned LLM response (`fixtures/canned_llm_response.json`); writes nothing to StateStore
    - `--test-channels` bypasses ingestion/news/LLM and calls `Notifier.test_channels`; non-zero exit on any channel failure
    - `--backtest <YYYY-MM-DD>` loads `fixtures/backtest/<YYYY-MM-DD>.json` and replays the pipeline; never dispatches; never writes StateStore
    - Every log entry includes a `mode` field
    - _Requirements: 13.1, 13.2, 13.3, 13.4_

  - [ ]* 16.2 Write property test for dry-run / backtest non-mutation guarantees
    - **Property 32: Dry-run mode never dispatches and never writes the StateStore's already_alerted set**
    - **Validates: Requirements 13.1, 13.3**
    - File: `tests/property/test_modes.py`

  - [ ]* 16.3 Write unit tests for each mode
    - File: `tests/unit/test_modes.py`

- [ ] 17. Checkpoint — Ensure all component tests pass
  - Ensure all tests pass, ask the user if questions arise.

- [x] 18. Implement Agent orchestrator and wire all components together
  - [x] 18.1 Implement `agent/main.py` and `agent/agent.py` with the `Agent` class
    - CLI in `main.py` via `argparse`: `--dry-run`, `--no-llm`, `--test-channels`, `--backtest <DATE>`; default mode `live`
    - `Agent.run(mode)` execution flow: load/validate config → instantiate StateStore (verify reachability or exit 1) → acquire `run_lock` (exit 0 with warning if held) → TimeGate active-hours check (exit 0 if outside) → resolve watchlist via `WatchlistSource.fetch()` (falls back to `watchlist_cache` on Notion failure per Requirement 1.6; exit 0 if no watchlist obtainable) → fetch prices + earnings calendar → fetch news → LLM process with CostGuard → DedupFilter.filter → build Alert (merging any `suppressed_alert`) → TimeGate quiet-hours check (persist `suppressed_alert` and exit 0 if inside) → Notifier.dispatch → on success DedupFilter.record_dispatched and clear `suppressed_alert` → write run summary → release run_lock
    - Inject `Logger`, `ConfigLoader`, `StateStore`, `WatchlistSource`, `TimeGate`, `IngestionModule`, `NewsFetcher`, `LLMProcessor`, `CostGuard`, `DedupFilter`, `AlertBuilder`, `Notifier` via constructor
    - Generate `run_id` (UUID4) at startup; bind it and `mode` to the logger
    - Handle the full error classification table: exit 1 for config/credential/state-store-unreachable; exit 0 for all others (including Notion-fetch-failed-with-no-cache)
    - Ensure `run_lock` is released on every exit path (try/finally)
    - _Requirements: 1.1, 1.6, 1.8, 2.2, 2.3, 2.4, 8.2, 8.3, 9.1, 9.2, 11.2, 11.6_

  - [x] 18.2 Add GitHub Actions workflow for scheduled deployment
    - Create `.github/workflows/agent.yml` per the skeleton in `design.md`'s Deployment Architecture section: `schedule` trigger (default `*/30 * * * *`) plus `workflow_dispatch`, a `concurrency` group as a secondary overlap guard, `actions/checkout` + `actions/setup-python`, `pip install -r requirements.txt`, then `python -m agent.main`
    - Document the agent's internal active-hours guard as authoritative (the GitHub Actions cron runs in UTC and does not need to match market hours)
    - Document that the default `state_store.type: redis` requires `STATE_STORE_REDIS_URL` (e.g. Upstash free tier) since GitHub Actions runners have no persistent volume
    - Document setting all sensitive env vars (`OPENAI_API_KEY`, `NOTION_API_TOKEN`, `STATE_STORE_REDIS_URL`, channel credentials) as GitHub Actions repository secrets (do NOT commit)
    - _Requirements: 2.1, 2.3, 10.3, 11.1_

  - [ ]* 18.3 Write integration test for end-to-end run with minimal watchlist
    - Tag with `@pytest.mark.integration`; skip in CI by default
    - Test a single-ticker `live` run with mocked external calls and a `MemoryStateStore`; verify cross-run dedup behavior across two back-to-back invocations
    - File: `tests/integration/test_yfinance_integration.py`
    - _Requirements: 1.1, 3.1, 4.1, 5.1, 6.1, 7.1, 11.4, 11.5_

- [ ] 19. Final checkpoint — Ensure all tests pass and pipeline is complete
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability
- Checkpoints ensure incremental validation at logical boundaries
- Property tests use Hypothesis with `@settings(max_examples=100)` minimum; each test references its design property number
- Unit tests use pytest with mocked external dependencies (yfinance, OpenAI, HTTP calls, Notion API)
- Integration tests are tagged `@pytest.mark.integration` and skipped in CI by default
- Sensitive credentials (API keys, tokens) are never written to config files; always read from environment variables (populated from GitHub Actions repository secrets in the deployed workflow)

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0,  "tasks": ["1.1"] },
    { "id": 1,  "tasks": ["2.1", "3.1"] },
    { "id": 2,  "tasks": ["2.2", "2.3", "2.4", "2.5", "3.2", "3.3", "3.4"] },
    { "id": 3,  "tasks": ["4.1"] },
    { "id": 4,  "tasks": ["4.2", "4.3", "4.4"] },
    { "id": 5,  "tasks": ["4.5", "5.1", "5.2"] },
    { "id": 6,  "tasks": ["4.6", "4.7", "5.3", "5.4", "5.5", "5.6"] },
    { "id": 7,  "tasks": ["6.1"] },
    { "id": 8,  "tasks": ["6.2", "6.3"] },
    { "id": 9,  "tasks": ["6.4", "6.5", "6.6", "6.7"] },
    { "id": 10, "tasks": ["8.1", "10.1"] },
    { "id": 11, "tasks": ["8.2", "9.1", "10.2"] },
    { "id": 12, "tasks": ["8.3", "8.4", "9.2", "9.3", "10.3", "10.4", "10.5", "10.6", "10.7"] },
    { "id": 13, "tasks": ["12.1", "13.1", "15.1"] },
    { "id": 14, "tasks": ["12.2"] },
    { "id": 15, "tasks": ["12.3", "12.4", "12.5", "12.6", "12.7", "12.8", "13.2", "13.3", "13.4", "14.1", "15.2", "15.3", "15.4"] },
    { "id": 16, "tasks": ["14.2", "14.3", "14.4", "14.5", "16.1"] },
    { "id": 17, "tasks": ["16.2", "16.3"] },
    { "id": 18, "tasks": ["18.1"] },
    { "id": 19, "tasks": ["18.2", "18.3"] }
  ]
}
```
