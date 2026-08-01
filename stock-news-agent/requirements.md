# Requirements Document

## Introduction

The Stock News Agent is an automated, scheduled system that monitors a user-defined ticker watchlist, identifies high-impact corporate and macro events, correlates those events with observed price behavior, and delivers a concise, high-signal alert to the user's preferred notification channel. It runs on a cron schedule as a GitHub Actions workflow, uses yfinance for price and baseline news data, supplements with additional free news sources, and uses a pay-as-you-go LLM to deduplicate stories, filter noise, and produce human-readable summaries.

The Agent persists a small amount of state between runs (Already_Alerted_Set, suppressed quiet-hours alerts, daily LLM cost ledger) so that the same Event is not re-alerted on every scheduled run. All times in configuration use IANA timezone identifiers; all logs are emitted in UTC.

## Glossary

- **Agent**: The Stock News Agent system as a whole.
- **Watchlist**: The user-defined set of ticker symbols the Agent monitors, stored in a static configuration file.
- **Ticker**: A stock or asset symbol (e.g., AAPL, TSLA) present in the Watchlist.
- **Event**: A high-impact corporate or macro occurrence (earnings release, regulatory ruling, product launch, analyst upgrade/downgrade, macroeconomic announcement) associated with a Ticker.
- **Price_Window**: The configurable time window (default: 2 hours) immediately following an Event used to evaluate market reaction.
- **Price_Movement**: The percentage change in a Ticker's price within the Price_Window after an Event.
- **Alert**: The formatted, deduplicated, high-signal summary payload delivered to the user.
- **Delivery_Channel**: The user-configured output target for Alerts (email, SMS, or webhook-based channel such as Telegram or Discord).
- **Scheduler**: The GitHub Actions scheduled workflow that triggers the Agent at configured intervals by running it on a fresh, ephemeral runner.
- **Watchlist_Source**: The configured origin of the Watchlist for a given run: either a static YAML/JSON config file, or a live query against a Notion database filtered by an "include" checkbox property.
- **Ingestion_Module**: The component responsible for loading the Watchlist and fetching raw price data via yfinance.
- **News_Fetcher**: The component responsible for querying multiple news sources for Ticker-relevant stories within a configurable lookback window.
- **LLM_Processor**: The component that uses a pay-as-you-go LLM (e.g., GPT-4o-mini or Gemini Flash) to deduplicate stories, filter noise, extract key facts, and link Events to Price_Movements.
- **Notifier**: The component that formats the final Alert and dispatches it to the configured Delivery_Channel.
- **Quiet_Hours**: A user-configurable time range during which the Agent suppresses Alert delivery.
- **Deduplication**: The process of identifying and collapsing multiple news stories that describe the same underlying Event (within a run and across runs).
- **Lookback_Window**: The configurable time period (default: 24 hours) used by the News_Fetcher to retrieve recent stories.
- **State_Store**: A persistent key-value store (Redis by default, e.g. Upstash's free tier, since the Agent runs on ephemeral GitHub Actions runners with no persistent local disk; pluggable to SQLite for local/dev use or Postgres) used to retain cross-run state including the Already_Alerted_Set, suppressed alerts, daily cost ledger, run lock, and the last-known-good Notion watchlist cache.
- **Already_Alerted_Set**: The set of Event identity keys (e.g., normalized URL hash plus Ticker) that have been previously dispatched in an Alert, retained for a configurable TTL (default: 7 days).
- **Impact_Score**: An integer 0–10 produced by the LLM_Processor for each candidate Event, representing the estimated market-relevance impact. Events below the configured `impact_threshold` are filtered.
- **Dry_Run**: An operational mode in which the Agent runs the full pipeline up to alert formatting and prints the would-be Alert to stdout instead of dispatching to Delivery_Channels and (optionally) calling the LLM.
- **Backtest**: An operational mode in which the Agent replays the pipeline against a saved fixture of stories and prices for a historical date, used to evaluate prompt and threshold changes.
- **Timezone**: A required IANA timezone identifier (e.g., `America/New_York`) configured once; all human-facing time fields (active hours, quiet hours) are interpreted in this timezone.

---

## Requirements

### Requirement 1: Watchlist Configuration

**User Story:** As a user, I want to define my ticker watchlist either in a static configuration file or by toggling stocks on/off in my existing Notion portfolio database, so that the Agent monitors only the assets I care about without requiring code changes, and my watchlist stays in sync with a tool I already maintain.

#### Acceptance Criteria

1. THE Agent SHALL support two Watchlist_Sources, selected via the `watchlist_source` config key: `static` (a YAML or JSON configuration file) or `notion` (a live query against a configured Notion database).
2. WHEN `watchlist_source: static`, THE Agent SHALL load the Watchlist from the configured file at startup; WHEN the file is missing or unparseable, or when the file is present but does not contain a `watchlist` key, THE Agent SHALL log an error message that identifies the file path and the nature of the error, and SHALL exit without sending an Alert.
3. WHEN `watchlist_source: notion`, THE Agent SHALL query the Notion database identified by `notion.database_id` (via the Notion API, authenticated with the `NOTION_API_TOKEN` environment variable) at the start of every run, and SHALL build the Watchlist from pages where the configured boolean property (`notion.include_property`, default `Track in Agent`) is checked.
4. WHEN sourcing from Notion, THE Agent SHALL extract each Ticker symbol from the configured title property (`notion.title_property`, default `Name`) by matching the pattern `\(([A-Z0-9.]{1,10})\)`; pages whose title property does not contain a matching parenthesized ticker SHALL be logged as a warning identifying the page and excluded from the Watchlist, rather than causing the run to fail.
5. WHEN sourcing from Notion and a `notion.group_property` is configured (e.g. mapping to a Sector or Status select property), THE Agent SHALL use that property's value as the Ticker's group label in the Alert, consistent with static-config groups.
6. IF a Notion watchlist query fails (API error, timeout, or unreachable), THEN THE Agent SHALL log a warning, fall back to the most recently successful Notion watchlist snapshot cached in the State_Store (key `watchlist_cache`, no expiry, overwritten on every successful sync), and continue the run with that cached Watchlist; IF no cached snapshot exists, THEN THE Agent SHALL log an error and exit without sending an Alert (this is a recoverable, retry-next-run condition, not a fatal configuration error).
7. Regardless of Watchlist_Source, THE Watchlist SHALL support a minimum of 1 and a maximum of 50 Ticker symbols per run; each Ticker symbol SHALL consist of 1–10 uppercase alphanumeric characters with an optional exchange suffix (e.g., `.L`, `.TO`); duplicate Ticker symbols SHALL be deduplicated silently, retaining one entry.
8. WHEN a Ticker symbol in the Watchlist is not recognized by yfinance, THE Ingestion_Module SHALL log a warning identifying the unrecognized symbol and continue processing the remaining Tickers.
9. WHEN all Ticker symbols in the Watchlist have been skipped due to yfinance validation failures, THE Agent SHALL log an error stating that no valid Tickers remain and SHALL exit without sending an Alert.
10. THE Watchlist SHALL optionally support grouping Tickers under user-defined group names (e.g., `core_holdings`, `watchlist`, `macro`); when groups are present, THE Agent SHALL include the group label alongside each Ticker in the Alert; the union of all groups (deduplicated) SHALL be subject to the same 1–50 size limit.

---

### Requirement 2: Scheduled Execution

**User Story:** As a user, I want the Agent to run automatically on a defined schedule, so that I receive timely updates without manual intervention.

#### Acceptance Criteria

1. THE Scheduler SHALL be a GitHub Actions workflow (`.github/workflows/agent.yml`) triggered on one or more `schedule` cron expressions (default: three per trading day — just after the open, just after the close, and in the evening for post-close earnings — evaluated in UTC per GitHub Actions semantics) plus a `workflow_dispatch` trigger for on-demand manual runs; the Agent additionally enforces active-hours via the configured `timezone` and `active_hours_start`/`active_hours_end`, which acts as a safety net against delayed or stray fires and absorbs the seasonal drift a UTC cron cannot follow.
2. WHEN the Agent is triggered outside the configured active hours (default: 09:30–17:00 in the configured `timezone`), THE Agent SHALL skip ingestion, LLM processing, and dispatch, and SHALL write a log entry with severity `info` stating the run was suppressed and the current time in both the configured timezone and UTC.
3. THE Scheduler SHALL run each invocation on a fresh, ephemeral GitHub Actions runner with no persistent local disk between runs; each invocation SHALL start a fresh process, complete its work, and exit; all cross-run state SHALL live exclusively in the external State_Store, never on the runner's local filesystem.
4. WHEN a scheduled trigger fires while a previous Agent run is still executing, THE Agent SHALL detect the in-progress run via a lock record in the State_Store (with a TTL of 10 minutes), skip execution, and log a `warning` entry indicating the missed fire, the time it occurred, and the `run_id` of the in-progress run; the GitHub Actions workflow SHOULD additionally set a `concurrency` group as a first line of defense, but the State_Store lock remains the authoritative guard (e.g. against an overlapping manual `workflow_dispatch` run).

---

### Requirement 3: Price Data Ingestion

**User Story:** As a user, I want the Agent to fetch current and recent price data for each watchlisted ticker, so that price movements can be correlated with news events.

#### Acceptance Criteria

1. WHEN the Agent is triggered, THE Ingestion_Module SHALL fetch intraday price data at 5-minute intervals for the current trading day and daily OHLCV data for the trailing 5 trading days for each Ticker in the Watchlist using the yfinance library.
2. THE Ingestion_Module SHALL retrieve at minimum the open, high, low, close, and volume fields for each Ticker; IF any of these fields are missing or null for a given interval, THEN THE Ingestion_Module SHALL log a warning identifying the Ticker, the interval, and the missing field, and SHALL use the available fields for that interval.
3. IF yfinance returns no price data at all for a Ticker, THEN THE Ingestion_Module SHALL log a warning for that Ticker and exclude it from further processing in that run.
4. WHEN an Event is identified for a Ticker and the Event publication timestamp falls within regular market hours of a trading session, THE Ingestion_Module SHALL calculate the Price_Movement as the percentage change from the close price at the Event publication timestamp (or the nearest preceding intraday bar) to the close price at the end of the Price_Window; IF the Price_Window extends beyond available intraday data, THEN THE Ingestion_Module SHALL use the last available price within the window and note the truncation in the log.
5. WHEN an Event publication timestamp falls outside regular market hours, THE Ingestion_Module SHALL anchor the Price_Movement calculation as follows:
   - **Pre-market (before 09:30 in market timezone)**: from the prior trading session's close to the current session's open + Price_Window (or last available bar, whichever is earlier).
   - **After-hours (after 16:00 in market timezone) or weekend/holiday**: from the most recent trading-session close to the next trading-session open + Price_Window (or last available bar). IF the next session has not yet opened at processing time, THEN THE Ingestion_Module SHALL emit the Event without a Price_Movement and set `price_movement_pending: true` so the next run can compute and reconcile it.
6. WHEN the Agent is triggered outside regular market hours and no intraday data is available for the current session, THE Ingestion_Module SHALL use the most recent available daily close price and log an `info` entry indicating that market-hours data is unavailable.
7. THE Ingestion_Module SHALL additionally fetch the upcoming earnings date (if available) for each Ticker via yfinance's calendar API; the earnings date SHALL be attached to the Ticker's data and surfaced in the Alert when an earnings event is within the next 7 calendar days.

---

### Requirement 4: Multi-Source News Fetching

**User Story:** As a user, I want the Agent to pull news from multiple free sources, so that coverage is broad and no single high-impact story is missed.

#### Acceptance Criteria

1. THE News_Fetcher SHALL query at least two distinct news sources per Ticker per run, including at minimum yfinance's built-in news feed and one of the following: Google News RSS, Finviz news, or Yahoo Finance RSS.
2. THE News_Fetcher SHALL support configuration of additional free sources beyond the minimum two, including at minimum Google News RSS, Finviz news, and Yahoo Finance RSS feeds.
3. THE News_Fetcher SHALL restrict fetched stories to those published within the Lookback_Window.
4. WHEN a news source is unavailable or does not respond within 30 seconds, THE News_Fetcher SHALL log a `warning` entry identifying the source name and the error, and SHALL continue fetching from remaining configured sources.
5. THE News_Fetcher SHALL associate each fetched story with the Ticker it was retrieved for, the story's publication timestamp, and the story's source URL; IF a story does not include a URL, THEN THE News_Fetcher SHALL record the source name in place of the URL.
6. WHEN all configured news sources fail for a given Ticker, THE News_Fetcher SHALL log an `error` entry for that Ticker, return an empty story list for that Ticker, and continue processing remaining Tickers.
7. THE News_Fetcher SHALL deduplicate stories within a single run by discarding any story whose URL exactly matches a previously fetched story's URL, or whose combination of Ticker symbol, headline, and publication timestamp matches a previously fetched story.

---

### Requirement 5: LLM-Based Event Processing

**User Story:** As a user, I want the Agent to use an LLM to filter noise, deduplicate stories, and extract only high-impact events, so that I receive a concise and actionable summary rather than a raw news dump.

#### Acceptance Criteria

1. WHEN the LLM_Processor receives a set of stories for a Ticker, THE LLM_Processor SHALL deduplicate stories that describe the same underlying Event across multiple sources by retaining the story with the greatest word count and appending all unique source URLs from the discarded duplicates to the retained story.
2. THE LLM_Processor SHALL request structured output from the LLM (e.g., OpenAI `response_format: json_schema` or equivalent function-calling) conforming to a fixed Event schema containing: `headline`, `summary` (≤150 words), `category` (one of the configured `high_impact_categories`), `impact_score` (integer 0–10), `source_urls` (non-empty list), and `market_reaction` (string, optional).
3. WHEN evaluating stories for a Ticker, THE LLM_Processor SHALL filter out Events whose `impact_score` is below the configured `impact_threshold` (default: 6) or whose `category` is not in the configured `high_impact_categories`; default categories SHALL be earnings releases, regulatory rulings, product launches, analyst rating changes, and macroeconomic announcements; both the threshold and categories SHALL be configurable.
4. WHEN at least one Event passes the impact threshold for a Ticker, THE LLM_Processor SHALL produce a concise Event summary of no more than 150 words per Event including at least one direct source URL.
5. WHEN an Event summary is produced for a Ticker and Price_Movement data is available for that Ticker within the Price_Window, THE LLM_Processor SHALL include in the `market_reaction` field a sentence characterizing the market's reception of the Event (e.g., positive, negative, muted) and contextualizing it against a configured benchmark index (default: `SPY` or `QQQ`) to distinguish idiosyncratic from macro-driven movement.
6. WHEN no Events pass the impact threshold for any Ticker in a run, THE LLM_Processor SHALL produce a "no significant events" summary of no more than 50 words covering all monitored Tickers.
7. THE LLM_Processor SHALL use a pay-as-you-go LLM API (configurable; default: `claude-haiku-4-5`); WHEN an LLM API call completes (success or failure), THE LLM_Processor SHALL log the `model`, `input_tokens`, `output_tokens`, `estimated_cost_usd`, and `run_id`.
8. IF the LLM API does not respond within 30 seconds or returns an error, THEN THE LLM_Processor SHALL wait 5 seconds and retry the request once; IF the retry also fails, THEN THE LLM_Processor SHALL log the error with severity `error` and skip LLM processing for that Ticker (not the whole run), continuing with remaining Tickers.
9. WHEN Price_Movement data is unavailable for a Ticker at the time of LLM processing, THE LLM_Processor SHALL produce the Event summary without the `market_reaction` field and SHALL note the absence of price data in the summary.
10. IF the LLM returns a response that fails JSON schema validation, THEN THE LLM_Processor SHALL retry once with the validation error appended to the prompt; IF the second attempt also fails validation, THEN THE LLM_Processor SHALL log an `error` entry and skip that Ticker.

---

### Requirement 6: Event Summary Format

**User Story:** As a user, I want each alert to present events in a structured, readable format, so that I can quickly understand what happened and why the market reacted.

#### Acceptance Criteria

1. THE Agent SHALL format each Alert as a structured payload containing for each Event: the Ticker symbol, the Ticker's group label (if grouped), the Event headline, the Impact_Score, a concise summary (≤150 words), the Price_Movement percentage over the Price_Window (or `null` with a reason), and at least one direct source URL.
2. Events SHALL be ordered by descending Impact_Score; ties SHALL be broken by descending absolute Price_Movement; remaining ties SHALL be broken by ascending alphabetical Ticker symbol.
3. WHEN multiple Tickers have Events in the same run, THE Agent SHALL group Events by Ticker in the Alert, with each Ticker appearing as a distinct labeled section; IF groups are configured, THEN Tickers SHALL be sub-grouped under their group label.
4. THE Agent SHALL include in the Alert header: a run timestamp in UTC ISO 8601 format, the same timestamp rendered in the configured timezone, and the total count of Tickers monitored in that run (including Tickers with no events).
5. WHEN any Ticker has an upcoming earnings date within 7 calendar days, THE Agent SHALL include an "Upcoming Earnings" section in the Alert listing each such Ticker and its earnings date.

---

### Requirement 7: Alert Delivery

**User Story:** As a user, I want to receive alerts on my phone and optionally view them in a readable format, so that I stay informed wherever I am.

#### Acceptance Criteria

1. THE Notifier SHALL support at least one of the following Delivery_Channels: email (via SMTP or a transactional email API), Telegram bot message, or Discord webhook.
2. THE Delivery_Channel SHALL be configurable via the static configuration file; each configured channel entry SHALL include all required connection parameters for that channel type (e.g., SMTP host/port/credentials for email, bot token and chat ID for Telegram, webhook URL for Discord).
3. WHEN an Alert is ready, THE Notifier SHALL dispatch it to all configured Delivery_Channels.
4. IF a Delivery_Channel dispatch fails with a transient error (timeout, 5xx, 429), THEN THE Notifier SHALL retry once after a 2-second backoff; IF the retry fails or the original failure is non-transient (4xx other than 429), THEN THE Notifier SHALL log the failure with the channel name and error details and SHALL attempt delivery to remaining configured channels.
5. IF the target Delivery_Channel supports markdown, THEN THE Notifier SHALL format the Alert using lightweight markdown (bold, bullet points, inline links); OTHERWISE THE Notifier SHALL format the Alert as plain text. Per-channel character limits SHALL be enforced as follows: Telegram body ≤4096 chars; Discord webhook content ≤2000 chars (use embeds, ≤6000 chars total, for longer content); email body unbounded; any subject or title field ≤200 chars across all channels.
6. WHEN zero Delivery_Channels are configured, THE Notifier SHALL log an `error` entry stating that no delivery channels are configured and SHALL exit without attempting dispatch.
7. THE Notifier SHALL support a `--test-channels` invocation that sends a canned verification message ("Stock News Agent: channel test, &lt;timestamp&gt;") to every configured channel and exits, used to verify channel credentials without running the pipeline.

---

### Requirement 8: Quiet Hours

**User Story:** As a user, I want to optionally suppress alert delivery during certain hours so I am not pinged outside times I care about.

#### Acceptance Criteria

1. THE Agent SHALL support an optional Quiet_Hours range (`quiet_hours_start`, `quiet_hours_end`) in HH:MM 24-hour format, interpreted in the configured `timezone`. Midnight-spanning ranges (e.g., 22:00–06:00) SHALL be supported.
2. WHILE the current time is within Quiet_Hours, THE Notifier SHALL withhold Alert delivery, persist the formatted Alert to the State_Store under key `suppressed_alert`, and log an `info` entry stating suppression and the configured range.
3. WHEN the current time is outside Quiet_Hours and a `suppressed_alert` exists in the State_Store, THE Agent SHALL merge that suppressed Alert with the current run's Alert (deduplicating Events by Event identity key), dispatch the merged Alert, and delete the State_Store entry on successful dispatch to at least one channel.
4. WHEN Quiet_Hours are not configured, THE Agent SHALL deliver Alerts at every active-hours run without suppression.

---

### Requirement 9: Observability and Logging

**User Story:** As a developer, I want structured run logs for every execution, so that I can diagnose failures and monitor the Agent's behavior over time.

#### Acceptance Criteria

1. THE Agent SHALL produce log entries as newline-delimited JSON (one JSON object per line) to stdout; each run-level log entry SHALL contain: `run_id`, `severity` (one of `info`, `warning`, `error`), `run_start_time`, `run_end_time`, `tickers_processed`, `stories_fetched_per_source`, `events_identified`, `delivery_channel_statuses` (each with a value of `success`, `failure`, or `skipped`), and an `errors` array.
2. WHEN an error occurs in any component, THE Agent SHALL log a JSON entry containing the `component` name, `error_type`, and a `message` field with a descriptive string, without crashing the overall run unless the error is unrecoverable; unrecoverable errors are defined as: missing required credentials, inability to reach the LLM API after all retries, or failure to load the configuration file.
3. WHEN an LLM API call completes or fails, THE Agent SHALL log a JSON entry containing: `run_id`, `model`, `input_tokens`, `output_tokens`, `estimated_cost_usd`, and `severity`.
5. THE Agent SHALL write a single run-summary log entry at run completion containing `daily_cost_usd_running_total` so cost can be monitored from the GitHub Actions run log alone.
4. THE Agent SHALL write all log output exclusively to stdout so that the GitHub Actions run log captures it without additional configuration.

---

### Requirement 10: Configuration Management

**User Story:** As a developer, I want all runtime parameters to be defined in a single configuration file and environment variables, so that the Agent can be reconfigured without modifying source code.

#### Acceptance Criteria

1. THE Agent SHALL read all configurable parameters — including `watchlist_source` and its associated static-file or Notion settings, Delivery_Channel settings, Quiet_Hours, Lookback_Window, Price_Window, LLM model name, cron schedule, `timezone`, `impact_threshold`, `daily_cost_cap_usd`, `max_input_tokens_per_run`, `benchmark_index`, and State_Store backend configuration — from a single YAML or JSON configuration file; WHEN the same parameter is defined in both the configuration file and an environment variable, THE Agent SHALL use the environment variable value, treating environment variables as having higher precedence.
2. WHEN a required configuration value is missing, THE Agent SHALL log an error to stderr identifying the missing key by name and SHALL exit with a non-zero exit code without processing.
3. THE Agent SHALL support storing sensitive values (API keys, SMTP credentials, bot tokens) exclusively in environment variables, never in the configuration file. THE deployment documentation SHALL explicitly direct users to set these as GitHub Actions repository secrets rather than committing them.
4. THE Agent SHALL validate all configuration values at startup and report all validation errors to stderr before beginning any data fetching; WHEN one or more validation errors are found, THE Agent SHALL exit with a non-zero exit code after reporting all errors.
5. THE Agent SHALL enforce the following validation rules: Watchlist length between 1 and 50 entries (after merging groups); Lookback_Window and Price_Window as positive integers; Quiet_Hours times in HH:MM 24-hour format; `timezone` as a valid IANA timezone identifier resolvable via `zoneinfo`; cron schedule as a valid 5-field cron expression; Delivery_Channel type as one of the supported values (`email`, `telegram`, `discord`); `impact_threshold` as an integer 0–10; `daily_cost_cap_usd` as a positive number.

---

### Requirement 11: Persistent State and Cross-Run Deduplication

**User Story:** As a user, I want the Agent to remember which Events it has already alerted me about, so that the same earnings story is not re-sent on every scheduled run within the Lookback_Window.

#### Acceptance Criteria

1. THE Agent SHALL maintain a State_Store with at least the following backends, selected via configuration: (a) `redis` (default; e.g. Upstash's free tier via connection string in an environment variable — required because the Agent runs on ephemeral GitHub Actions runners with no persistent disk between invocations), (b) `sqlite` (local file, intended for local development and testing only, not for the GitHub Actions deployment), (c) `memory` (test-only, non-persistent).
2. THE State_Store SHALL support the following named records: `already_alerted` (a set of Event identity keys with per-key TTLs), `suppressed_alert` (an Alert blob persisted across runs during Quiet_Hours), `run_lock` (a record holding `run_id` and lock acquisition timestamp with a 10-minute TTL), `daily_cost_ledger` (a per-UTC-date running total of `estimated_cost_usd`), and `watchlist_cache` (the most recent successfully-fetched Notion watchlist, used as a fallback when the Notion API is unreachable; no TTL, overwritten on every successful sync).
3. THE Event identity key SHALL be computed as `sha256(normalized_url)` when a URL is present, OR `sha256(ticker + lowercased_headline + ISO8601_date(published_at))` when no URL is available; the chosen scheme SHALL be deterministic and stable across runs.
4. WHEN the LLM_Processor produces a candidate Event, THE Agent SHALL skip the Event from the outgoing Alert IF its identity key is already present in `already_alerted`; the skipped Event SHALL be logged at `info` severity.
5. WHEN an Alert is successfully dispatched to at least one Delivery_Channel, THE Agent SHALL insert every dispatched Event's identity key into `already_alerted` with a TTL equal to the configured `already_alerted_ttl_hours` (default: 168 = 7 days).
6. IF the configured State_Store backend is unreachable at run start, THEN THE Agent SHALL log an `error` and exit with a non-zero code WITHOUT attempting LLM processing or dispatch (rather than silently disabling deduplication, which would cause alert spam).
7. THE State_Store backend SHALL be selected via the `state_store.type` config key and configured via `state_store.path` (sqlite) or environment variables (redis connection string).

---

### Requirement 12: LLM Cost Guardrails

**User Story:** As a user, I want hard caps on LLM spend so a misconfiguration cannot run up an unexpected bill.

#### Acceptance Criteria

1. THE Agent SHALL enforce a configurable `max_input_tokens_per_run` (default: 100,000); WHEN the cumulative input tokens for a run would exceed this cap, THE Agent SHALL stop sending Tickers to the LLM, log a `warning`, and proceed to dispatch with whatever Events were already produced.
2. THE Agent SHALL enforce a configurable `daily_cost_cap_usd` (default: 1.00); WHEN the `daily_cost_ledger` for the current UTC date plus the projected cost of the next LLM call would exceed the cap, THE Agent SHALL skip remaining LLM calls for the run, log a `warning` identifying the cap and current ledger value, and proceed to dispatch with whatever Events were already produced.
3. THE Agent SHALL maintain a per-model cost table (USD per 1M input/output tokens) in the configuration with sensible defaults for the configured model; `estimated_cost_usd` for each call SHALL be computed from this table.
4. WHEN the configured model is not present in the cost table, THE Agent SHALL log a `warning` at startup and treat `estimated_cost_usd` as 0 for that model (no cap enforcement possible).

---

### Requirement 13: Operational Modes (Dry-Run, Test-Channels, Backtest)

**User Story:** As a developer, I want operational modes that let me verify configuration and iterate on the LLM prompt without burning API tokens or spamming notification channels.

#### Acceptance Criteria

1. THE Agent SHALL support a `--dry-run` CLI flag that runs the full pipeline through LLM processing (or with `--dry-run --no-llm`, substitutes a deterministic canned response) and prints the would-be Alert JSON to stdout instead of dispatching to any Delivery_Channel.
2. THE Agent SHALL support a `--test-channels` CLI flag that bypasses ingestion, news, and LLM, and dispatches a canned verification message to every configured Delivery_Channel; exit code 0 SHALL indicate all channels accepted, non-zero SHALL indicate at least one failure.
3. THE Agent SHALL support a `--backtest <YYYY-MM-DD>` CLI flag that loads a fixture file at `fixtures/backtest/<YYYY-MM-DD>.json` containing stories and price data, runs the pipeline against it, and writes the resulting Alert to stdout; the backtest mode SHALL NOT dispatch to channels and SHALL NOT write to the State_Store.
4. WHEN any operational mode is active, THE Agent SHALL include a `mode` field (`dry_run`, `test_channels`, `backtest`, or `live`) in every log entry.

---

### Requirement 14: News Adapter Resilience and Snapshot Coverage

**User Story:** As a developer, I want news source adapter brittleness to be caught by tests, so that an upstream RSS/HTML change does not silently degrade the agent.

#### Acceptance Criteria

1. THE News_Fetcher SHALL ship with at least one snapshot/contract test per adapter (yfinance, Google News RSS, Finviz, Yahoo Finance RSS) using a saved sample payload checked into the repo under `tests/fixtures/news/`.
2. WHEN an adapter's output schema (e.g., field names, presence of `link`/`published`) drifts from its snapshot, THE adapter's snapshot test SHALL fail loudly with a diff identifying the changed fields.
3. THE News_Fetcher SHALL log a structured `warning` entry with `adapter_name`, `parse_failure_count`, and a sample of the unparseable payload (truncated to 500 chars) when ≥10% of fetched items from a source fail to parse, even if some items succeed.
