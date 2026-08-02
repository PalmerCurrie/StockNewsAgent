# Stock News Agent

Watches a list of tickers, pulls news and price data, uses an LLM to throw away
the noise, and sends you a short alert with only the things that actually
moved. Runs as a short-lived process — start it from cron, a scheduler, or by
hand.

<img width="864" height="871" alt="image" src="https://github.com/user-attachments/assets/80a43992-89aa-44a1-8bcf-1026fc8ef796" />


You need two things: **a list of tickers** and **one LLM API key**.

## See it work first (no key, no accounts, no network)

```bash
pip install -r requirements.txt
python -m agent.main --backtest 2026-07-24 --no-llm
```

That replays a frozen day of inputs through the whole pipeline with a canned
LLM response and prints the alert it would have sent. Nothing is fetched,
nothing is sent, nothing is written.

## Set it up

**1. Your tickers.**

```bash
cp watchlist.example.yaml watchlist.yaml
```

Then edit it. The whole file can be this:

```yaml
- AAPL
- MSFT
- NVDA
```

Add `group:` to any entry you want to appear under a heading in the alert:

```yaml
- symbol: AAPL
  group: core holdings
- SPY
```

`watchlist.yaml` is gitignored, so your positions never end up in a commit.

**2. An LLM key.** Anthropic by default:

```bash
cp .env.example .env    # then fill in ANTHROPIC_API_KEY
```

**No, it doesn't have to be Claude.** OpenAI works too — set `llm_provider:
openai` and an OpenAI `llm_model` in `config.yaml`, and put `OPENAI_API_KEY` in
`.env` instead. The provider is inferred from the model name (`claude-*` /
`gpt-*`) if you don't set it explicitly. Adding a third provider means one
subclass in `agent/llm_backends.py`; nothing above that layer knows or cares.

Now you can do a full live run that fetches real news but never sends anything:

```bash
python -m agent.main --dry-run
```

That costs a few cents. Default model is `claude-haiku-4-5`, which runs about
**$0.04 per run** on a dozen tickers.

**3. Somewhere to send it.** Pick at least one and put its credentials in
`.env`, then list it under `channels:` in `config.yaml`:

| Channel | What you need |
|---|---|
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — talk to [@BotFather](https://t.me/BotFather), then read the chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates` |
| Discord | `DISCORD_WEBHOOK_URL` — Server Settings → Integrations → Webhooks |
| Email | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_TO` |

```bash
python -m agent.main --test-channels   # sends a canary; non-zero if any channel fails
python -m agent.main                   # the real thing
```

That's the whole setup. There is no database to provision and no account to
create beyond your LLM provider and your delivery channel.

## Scheduling

The agent does not daemonize — it runs once and exits, so use whatever your
machine already has:

```bash
# Linux/macOS crontab: 08:00 and 14:00, weekdays
0 8,14 * * 1-5  cd /path/to/StockNewsAgent && ./.venv/bin/python -m agent.main

# Windows
schtasks /create /tn "StockNewsAgent" /tr "C:\path\to\python.exe -m agent.main" /sc weekly /d MON,TUE,WED,THU,FRI /st 08:00
```

If you want the message to arrive at an exact minute regardless of how long the
run takes, fire the run early and let it hold the finished alert:

```bash
python -m agent.main --deliver-at 08:00 --deliver-tz America/New_York
```

Started at 07:45, that fetches immediately and delivers at 08:00:00. The hold
sits *after* the fetch, so waiting never costs freshness. If the target has
already passed it sends at once; if the target is more than 30 minutes away it
warns and sends anyway rather than burning half an hour to deliver stale news.

`.github/workflows/agent.yml` is an optional GitHub Actions deployment. Read the
comment at the top before enabling it: runners have no persistent disk (so it
needs Redis instead of SQLite), and on a public repo the logs publish your
watchlist. `triggers/railway/` is a worked example of hitting an exact delivery
time from a hosted scheduler.

## How a run works

```
run_lock -> active-hours gate -> read watchlist
  -> prices + earnings (yfinance) -> news (yfinance / Google News / Finviz / Yahoo RSS)
  -> LLM structured extraction -> cross-run dedup -> build alert
  -> quiet-hours gate -> send -> record what was sent -> run summary
```

Everything is fail-soft: a dead news source, an unresolvable ticker, or an LLM
failure skips that item and the run continues. Only configuration problems,
missing credentials and an unreachable state store exit non-zero.

Exit codes: `0` success, `1` configuration or credential problem, `2` the run
completed but **every delivery channel refused the alert**. A `2` is
deliberately non-zero — an agent that cannot reach you is not healthy, and a
green run that delivered nothing is the failure mode that hides longest.

| Path | What |
|---|---|
| `agent/main.py` | CLI entry point |
| `agent/agent.py` | Orchestrator + wiring |
| `agent/config_loader.py` | Config load, `AGENT_*` env overrides, validation |
| `agent/ingestion_module.py` | Prices, earnings, price-movement anchoring |
| `agent/news_fetcher.py` | Four news adapters, lookback filtering, in-run dedup |
| `agent/llm_processor.py` | Structured-output prompt, retries, cost guarding |
| `agent/llm_backends.py` | Anthropic and OpenAI backends |
| `agent/dedup_filter.py` | Cross-run "have I already sent this?" |
| `agent/alert_builder.py` | Ordering, grouping, quiet-hours merge |
| `agent/notifier.py` | Telegram / Discord / email formatting and delivery |
| `agent/state_store/` | SQLite (default), Redis, memory (tests) |

## Tuning what you get told about

Everything lives in `config.yaml`, and every key can be overridden by an
`AGENT_*` environment variable, which always wins.

| Key | Default | Effect |
|---|---|---|
| `impact_threshold` | `6` | 1–10. Raise it for fewer, bigger alerts |
| `high_impact_categories` | six categories | An event must match one of these *and* clear the threshold |
| `lookback_window_hours` | `24` | How far back to look for stories |
| `llm_model` | `claude-haiku-4-5` | Any Anthropic or OpenAI model |
| `active_hours_start` / `_end` | `08:30`–`22:00` | Safety net; a run starting outside this does nothing |
| `quiet_hours` | unset | Holds an alert landing in the window and merges it into the next |

Config is validated in one pass at startup: every error is printed before the
process exits 1, and nothing is fetched until it passes.

## Why there's a state store

It remembers, across runs, what it already told you about and what it already
paid to analyze. Without it the 24-hour lookback re-sends and re-bills the same
story on every run.

| Record | Answers |
|---|---|
| `already_alerted` | "have I told you about this?" — written when an alert is delivered |
| `analyzed_stories` | "have I paid to analyze this?" — written when a run reaches a conclusion, delivered or not |
| `daily_cost_ledger` | per-UTC-day LLM spend, so the cost cap survives across runs |
| `run_lock` | stops two runs overlapping |

The first two look redundant and are not: collapsing them ties spend
suppression to a successful delivery, so a stretch of runs that find nothing
worth sending keeps re-buying the same analysis. Neither is written when every
channel fails, so an undelivered alert is retried rather than lost.

**The default backend is SQLite** — one file, standard library, nothing to
provision. Redis (`state_store.type: redis`, `pip install redis`) exists for
hosts with no persistent disk, like CI runners.

## Cost control

Two caps in `config.yaml`: `cost.max_input_tokens_per_run` (default 100,000)
and `cost.daily_cost_cap_usd` (default $1.00, a per-UTC-day ledger shared by
every run that day). Both are checked against a projection before each call, so
the realized total can overshoot by at most the one call that tripped the cap.
A model missing from `cost.model_pricing` is logged at startup and **cannot be
capped**, so keep the model you use listed there.

Two things matter more than the caps, because they reduce the normal case
rather than just stopping a runaway:

1. **The model filters before it writes.** `impact_threshold` and
   `high_impact_categories` are stated in the prompt, so the model omits events
   it knows would be discarded instead of summarizing them first. Output tokens
   cost 5x input and were ~74% of the bill; on a measured 4-ticker run this cut
   output from 13,944 tokens to 663 (**−95%**) and halved cost per run. Python
   re-applies both filters afterwards regardless — the prompt is an
   optimization, not the enforcement point.
2. **Analyzed stories are remembered**, so the heavy overlap between a 24-hour
   lookback and a few hours between runs is not re-bought.

## Running it

```bash
python -m agent.main                         # live run
python -m agent.main --dry-run               # full pipeline, prints the alert, never sends
python -m agent.main --dry-run --no-llm      # same, canned LLM response, no state writes
python -m agent.main --test-channels         # canary to every channel
python -m agent.main --backtest 2026-07-24   # replay fixtures/backtest/2026-07-24.json
python -m agent.main --config other.yaml     # alternate config file
```

`--test-channels` deliberately sends through the same formatter a real alert
uses, with a fixture carrying every MarkdownV2 reserved character
(`` _*[]()~`>#+-=|{}.! ``), a URL with parentheses and query separators, and a
signed decimal. An earlier plain-text version of this check reported success for
a month while every real alert was being rejected for an unescaped `|`. A
channel test that does not exercise the real formatter only proves the
credentials are good.

## Logs

Newline-delimited JSON on stdout. Every entry carries `run_id`, `mode`,
`severity`, `component` and `message`; each run ends with one `run_summary`
containing tickers processed, per-source story counts, event counts, delivery
statuses and the running daily spend.

## Disclaimer

This produces automated summaries of public news for your own reading. It is not
financial advice, it makes no recommendations, and an LLM summarising a headline
can be wrong or incomplete. Do not trade off it without checking the source
links in the alert.

## License

MIT — see [LICENSE](LICENSE).
