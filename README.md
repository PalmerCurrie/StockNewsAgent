# Stock News Agent

A scheduled pipeline that watches a ticker watchlist, pulls news and price data,
uses an LLM to filter noise down to high-impact events, and pushes a short alert
to Telegram / Discord / email. It runs as a short-lived process on GitHub
Actions; all cross-run memory lives in an external Redis.

Specs live in [`stock-news-agent/`](stock-news-agent/): `requirements.md`,
`design.md`, `tasks.md`.

## Try it without any credentials

The fastest way to see what it does. No API keys, no accounts, no network:

```bash
pip install -r requirements.txt
python -m agent.main --backtest 2026-07-24 --no-llm
```

That replays a frozen day of inputs through the whole pipeline with a canned
LLM response and prints the alert JSON it would have sent. Nothing is fetched,
nothing is dispatched, no state is written.

When you want to point it at your own tickers, edit the `watchlist:` list in
`config.yaml`, set `watchlist_source: static`, and add an LLM key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
python -m agent.main --dry-run          # live news, real LLM call, never dispatches
```

`--dry-run` costs a few cents. From there, [Setup](#setup) covers Notion for a
managed watchlist, Redis for cross-run memory, and a delivery channel.

## How a run works

```
run_lock -> active-hours gate -> resolve watchlist (Notion or static file)
  -> prices + earnings (yfinance) -> news (yfinance / Google News / Finviz / Yahoo RSS)
  -> LLM structured extraction -> cross-run dedup -> build alert (+ merge suppressed)
  -> quiet-hours gate -> dispatch -> record dispatched -> run summary
```

Everything is fail-soft: a dead news source, an unresolvable ticker, or an LLM
failure skips that item and the run continues. Only configuration problems,
missing credentials, and an unreachable state store exit non-zero.

## Layout

| Path | What |
|---|---|
| `agent/main.py` | CLI entry point (`python -m agent.main`) |
| `agent/agent.py` | Orchestrator + component wiring |
| `agent/config_loader.py` | YAML/JSON load, `AGENT_*` env overrides, validation |
| `agent/watchlist_source/` | `static` file source and `notion` database source |
| `agent/ingestion_module.py` | yfinance prices, earnings, price-movement anchoring |
| `agent/news_fetcher.py` | Four news adapters, lookback filtering, in-run dedup |
| `agent/llm_processor.py` | Structured-output prompt, retries, cost guarding |
| `agent/dedup_filter.py` | Cross-run `already_alerted` identity keys |
| `agent/alert_builder.py` | Ordering, grouping, quiet-hours merge |
| `agent/notifier.py` | Telegram / Discord / email adapters and formatting |
| `agent/state_store/` | Redis (deployed), SQLite (local), memory (tests) |
| `config.yaml` | All non-secret runtime settings |
| `fixtures/` | `canned_llm_response.json` (`--no-llm`) and `backtest/<date>.json` |

## Setup

### 1. Install

```bash
pip install -r requirements.txt
```

### 2. Notion (only for `watchlist_source: notion`)

Skip this entirely if you are happy keeping tickers in `config.yaml` — set
`watchlist_source: static` and move on.

1. Add a **Checkbox** property named `Track in Agent` to your stocks database
   and tick it for every ticker you want watched. (Change the name in
   `config.yaml` under `notion.include_property` if you prefer another.)
2. Create an internal integration at <https://www.notion.so/my-integrations>,
   copy its token, and share the database with it
   (database → ⋯ → Connections → your integration).
3. Set `NOTION_API_TOKEN` to that token.
4. Set `AGENT_NOTION_DATABASE_ID` to the database's id — the hex string in its
   URL, `notion.so/<workspace>/<DATABASE_ID>?v=...`. It lives outside
   `config.yaml` because it is yours rather than the project's. It is **not** a
   secret (useless without the token), so deployed it belongs in Actions →
   Variables, not Secrets.

Tickers are parsed out of the `Name` title property with
`\(([A-Z0-9.]{1,10})\)`, so `(AMD) Advanced Micro Devices` yields `AMD`. Rows
with no parseable ticker (e.g. `SpaceX IPO`) are logged and skipped, never fatal.

### 3. State store

Create a free [Upstash](https://upstash.com/) Redis database and set
`STATE_STORE_REDIS_URL` to its `rediss://` connection string. This is required
for the GitHub Actions deployment — runners have no disk that survives a run.

For local work, use SQLite instead:

```yaml
state_store:
  type: sqlite
  path: ./agent_state.db
```

### 4. Pick at least one delivery channel

| Channel | Env vars |
|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| Discord | `DISCORD_WEBHOOK_URL` |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TO` |

List the ones you want under `channels:` in `config.yaml`, then verify them:

```bash
python -m agent.main --test-channels
```

### 5. LLM API key

Default provider is Anthropic. Get a key at
<https://console.anthropic.com/settings/keys> and set `ANTHROPIC_API_KEY`. Make
sure the account has credit — a $0 balance fails at the LLM step with a quota
error.

To use OpenAI instead, set `llm_provider: openai` and an OpenAI `llm_model` in
`config.yaml`, and supply `OPENAI_API_KEY`.

| `config.yaml` | Default | Notes |
|---|---|---|
| `llm_model` | `claude-haiku-4-5` | Provider inferred from the name (`claude-*` / `gpt-*`) |
| `llm_provider` | `anthropic` | `anthropic` \| `openai`; overrides the inference |
| `llm_effort` | `medium` | Opus/Sonnet tiers only — thinking depth and token spend. Skipped automatically on Haiku, which rejects the parameter |
| `llm_max_output_tokens` | `8000` | Caps thinking **plus** response text on Claude models. Too low truncates mid-answer |

Model pricing (USD per 1M tokens) lives under `cost.model_pricing` and must list
the model you actually use — an unlisted model is logged at startup and its spend
**cannot be capped**:

| Model | Input | Output |
|---|---|---|
| `claude-opus-5` | $5.00 | $25.00 |
| `claude-sonnet-5` | $3.00 | $15.00 |
| `claude-haiku-4-5` | $1.00 | $5.00 |

### 6. Where the secrets go

Two places, same variable names — real environment variables always win, so a
local `.env` can never shadow a deployed secret.

**Bring your own keys.** Nothing in this repo contains a credential. Every key is
read from the environment at startup (`os.environ`, no defaults, no fallbacks)
and the run refuses to start if one is missing, so a clone of this repo cannot
use anyone else's account.

**GitHub Actions (deployed runs).** Settings → Secrets and variables → Actions →
New repository secret, one per value. The workflow maps them into the job:

```
ANTHROPIC_API_KEY  NOTION_API_TOKEN  STATE_STORE_REDIS_URL
TELEGRAM_BOT_TOKEN  TELEGRAM_CHAT_ID
```

Non-secret per-user settings go in the **Variables** tab beside it, not Secrets
— `AGENT_NOTION_DATABASE_ID`, and optionally `AGENT_IMPACT_THRESHOLD` and
`AGENT_ACTIVE_HOURS_START` / `_END`. Variables are readable in the UI, which
matters when you need to check whether one is wrong.

**If you run this from a public repo, note that Actions logs are public too.**
The run summary lists every ticker processed and your daily LLM spend. Keep the
deployment in a private repo if your watchlist is not something you want
indexed.

**`.env` (local runs only).** Copy `.env.example` to `.env` — it's gitignored and
loaded automatically at startup:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
NOTION_API_TOKEN=ntn_...
STATE_STORE_REDIS_URL=rediss://default:...@....upstash.io:6379
TELEGRAM_BOT_TOKEN=123456:ABC...
TELEGRAM_CHAT_ID=123456789
```

Point at a different file with `--env-file path/to/other.env`. Any `AGENT_*`
config override works there too (e.g. `AGENT_LLM_EFFORT=low`). Never commit it —
GitHub Actions reads secrets, not `.env`.

### Schedule, and why it is not a cron

**GitHub's `schedule` trigger cannot hit a target time.** It queues scheduled
workflows and creates the run when it has capacity. Measured on this repo:

| Cron (UTC) | Actually fired (UTC) | Late by |
|---|---|---|
| `0 14` | 16:00, 16:04 | 2h00, 2h04 |
| `50 14` | 16:11, 16:16, 16:26 | 1h21, 1h26, 1h36 |
| `50 20` | 21:40, 21:49, 21:48 | 0h50, 0h59, 0h58 |

Read the second row against the first: moving the cron 50 minutes *later* moved
the actual fire time only ~15 minutes. The delay is not a constant, so it cannot
be subtracted out — asking earlier just delivers at the wrong hour on a day the
queue happens to be quick.

A **dispatched** run has no such queue: it is created the instant the API call
lands. So the workflow's primary trigger is `workflow_dispatch`, driven by any
external scheduler (cron-job.org, Cloudflare Workers cron, a cloud scheduler, a
machine you own) that can POST:

```
POST https://api.github.com/repos/<owner>/<repo>/actions/workflows/agent.yml/dispatches
Authorization: Bearer <fine-grained PAT with Actions: Read and write>
Accept: application/vnd.github+json

{"ref":"main","inputs":{"deliver_at":"08:00","deliver_tz":"America/Los_Angeles"}}
```

Trigger ~15 minutes before you want the message. A run takes 2–8 minutes
depending on how much news there is, so the agent holds the finished alert until
`deliver_at` and the message lands on the exact minute regardless. The hold sits
*after* the fetch, so waiting never costs freshness.

**Daylight saving needs no seasonal edit**, as long as you schedule the external
trigger in a named timezone too. `08:00 America/Los_Angeles` is 08:00 in both
PST and PDT — there is no UTC hour field to remember to bump in November.

The `schedule:` crons still in the workflow are a **fallback**, not the
schedule. They pass no `deliver_at` and send as soon as they finish, so a dead
external trigger produces a late alert instead of silence. On a normal day the
dispatched run has already sent everything and cross-run dedup makes the
fallback a quiet no-op. If you see a fallback run actually deliver, your
external trigger has stopped firing.

`active_hours_start` / `_end` in `config.yaml` is a **safety net, not the
schedule** — the trigger already picks the times. Note it is checked at the
*start* of a run, before any hold, so it gates the fetch rather than the send.
It is set wide (08:30–22:00 ET) to admit both the primary trigger and the
fallback crons whenever GitHub gets to them. Narrowing it is how a late run
turns into no run at all. It still blocks a stray manual dispatch at 3am.

`quiet_hours` is deliberately unset: with two hand-picked times it could only
withhold the afternoon alert and merge it into the next morning's.

`cron_schedule` in `config.yaml` is informational only — keep it in sync with
the workflow's fallback crons so it does not describe a schedule the agent is
not on.

## Running it

```bash
python -m agent.main                         # live run
python -m agent.main --dry-run               # full pipeline, prints the alert JSON, never dispatches
python -m agent.main --dry-run --no-llm      # same, with a canned LLM response and no state writes
python -m agent.main --test-channels         # canned message to every channel; non-zero on failure
python -m agent.main --backtest 2026-07-24   # replay fixtures/backtest/2026-07-24.json
python -m agent.main --config other.yaml     # alternate config file

python -m agent.main --deliver-at 08:00 --deliver-tz America/Los_Angeles
```

`--deliver-at HH:MM` holds a finished live alert until that wall-clock time, so
delivery does not drift with however long the run took. `--deliver-tz` takes any
IANA zone and defaults to `timezone` from `config.yaml`; both also read
`AGENT_DELIVER_AT` / `AGENT_DELIVER_TZ`.

It never sleeps when it should not: if the target has already passed the alert
goes out immediately, and if the target is more than 30 minutes away it warns
and sends anyway — that means the trigger misfired, and holding would burn
runner minutes to deliver stale news. It applies only to live dispatch;
`--dry-run`, `--backtest` and `--test-channels` are unaffected.

Exit codes: `0` success, `1` configuration or credential problem, `2` the run
completed but **every delivery channel refused the alert**. A `2` is deliberately
non-zero so the workflow goes red — an agent that cannot reach you is not healthy,
and a green run that delivered nothing is the failure mode that hides longest.

`--test-channels` sends a **canary alert** through the same `send_alert` path a
real run uses — not plain text. The fixture is deliberately harder to render than
real news: it carries every MarkdownV2 reserved character
(`` _*[]()~`>#+-=|{}.! ``), a URL with parentheses and query separators, and a
signed decimal price move. Verified to fail on an unescaped separator, an
unescaped link destination, or missing field escaping.

This matters because the earlier plain-text version reported success for a month
while every real alert was being rejected for an unescaped `|`. A channel test
that does not exercise the real formatter only proves the credentials are good.

## Configuration

Every key in `config.yaml` can be overridden by an environment variable, and the
environment always wins. Nested keys flatten with underscores:

| Env var | Overrides |
|---|---|
| `AGENT_WATCHLIST_SOURCE` | `watchlist_source` |
| `AGENT_TIMEZONE` | `timezone` |
| `AGENT_IMPACT_THRESHOLD` | `impact_threshold` |
| `AGENT_LLM_MODEL` / `_PROVIDER` / `_EFFORT` | `llm_model`, `llm_provider`, `llm_effort` |
| `AGENT_ACTIVE_HOURS_START` / `_END` | active hours |
| `AGENT_QUIET_HOURS_START` / `_END` | quiet hours |
| `AGENT_CHANNELS` | `channels` (comma-separated types) |
| `AGENT_NEWS_SOURCES` | `news_sources` (comma-separated) |
| `AGENT_STATE_STORE_TYPE` / `_PATH` | `state_store.*` |
| `AGENT_COST_DAILY_COST_CAP_USD` | `cost.daily_cost_cap_usd` |
| `AGENT_NOTION_DATABASE_ID` etc. | `notion.*` |

Config is validated in one pass at startup; every error is printed to stderr
before the process exits 1, and nothing is fetched until it passes.

## Cost control

Two independent caps, both in `config.yaml`:

- `cost.max_input_tokens_per_run` (default 100,000) — stops sending tickers to
  the LLM for the rest of the run.
- `cost.daily_cost_cap_usd` (default $1.00) — a per-UTC-day ledger in the state
  store, shared across every run that day.

Both are checked before each call against a projection, so the realized total can
overshoot by at most the cost of the single call that tripped the cap. A model
missing from `cost.model_pricing` is logged at startup and cannot be capped.

Beyond the caps, three things keep the bill down. They matter more than the caps
do, because the caps only stop a runaway — these reduce the normal case:

1. **The model filters before it writes.** `impact_threshold` and
   `high_impact_categories` are stated in the prompt, so the model omits events
   that would be discarded rather than summarizing them first. Output tokens are
   5x the price of input and were ~74% of the bill; on a measured 4-ticker run
   this cut output from 13,944 tokens to 663 (**-95%**) and halved cost per run.
   Python re-applies both filters afterwards regardless — the prompt is an
   optimization, not the enforcement point.
2. **Analyzed stories are remembered** in the `analyzed_stories` record, so the
   heavy overlap between the 24h lookback and the ~4h gap between runs is not
   re-bought. This is deliberately separate from `already_alerted`: see below.
3. **Prompt caching is not used, on purpose.** Haiku 4.5 will not cache a prefix
   shorter than 4,096 tokens and the shared instruction block is ~400, so no
   cache entry would ever be created. Even if one were, it would apply only to
   input, the cheaper ~26% of the bill.

### `analyzed_stories` vs `already_alerted`

Two records that look redundant and are not:

| Record | Answers | Written when |
|---|---|---|
| `already_alerted` | "have I told you about this?" | an alert is delivered |
| `analyzed_stories` | "have I paid to analyze this?" | the run reaches a conclusion, delivered or not |

Collapsing them ties spend suppression to a successful delivery, so a stretch of
runs that find nothing worth sending — or cannot send — keeps re-buying the same
analysis every few hours. Neither is written when every channel fails, so an
undelivered alert is retried on the next run instead of being lost.

## Logs

Newline-delimited JSON on stdout, so the Actions log viewer picks it up with no
setup. Every entry carries `run_id`, `mode`, `severity`, `component`, and
`message`; each run ends with one `run_summary` entry containing tickers
processed, per-source story counts, event counts, delivery statuses, and the
running daily LLM spend.

## Testing

`tests/` is scaffolded for the unit / property / snapshot / integration suites
described in `design.md` and `tasks.md` (all still marked optional there).
`pytest.ini` deselects `@pytest.mark.integration` by default.

## Disclaimer

This produces automated summaries of public news for your own reading. It is not
financial advice, it makes no recommendations, and an LLM summarising a headline
can be wrong or incomplete. Do not trade off it without checking the source
links in the alert.

## License

MIT — see [LICENSE](LICENSE).
