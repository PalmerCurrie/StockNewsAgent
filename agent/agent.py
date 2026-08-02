"""The Agent orchestrator: one invocation, one linear pipeline.

Execution order (design.md, Requirement 2/8/11):

    run_lock -> active-hours gate -> watchlist -> prices+earnings -> news ->
    LLM -> cross-run dedup -> build alert (+ merge suppressed) ->
    quiet-hours gate -> dispatch -> record dispatched -> run summary

Every exit path releases the lock and writes exactly one run-summary entry.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Optional

from .alert_builder import AlertBuilder
from .cost_guard import CostGuard
from .dedup_filter import DedupFilter
from .ingestion_module import IngestionModule, calculate_price_movement
from .llm_backends import MissingAPIKeyError
from .llm_processor import LLMBudgetExhausted, LLMProcessor, StoryPriceContext
from .logger import Logger
from .models import (
    Alert,
    AgentConfig,
    Event,
    PriceData,
    RunMode,
    RunSummary,
    Story,
    TickerEntry,
)
from .modes import BacktestFixture, ModeFlags, load_backtest_fixture, load_canned_llm_response
from .news_fetcher import NewsFetcher
from .notifier import Notifier
from .state_store import build_state_store
from .state_store.memory_store import MemoryStateStore
from .state_store.base import (
    KEY_RUN_LOCK,
    KEY_SUPPRESSED_ALERT,
    RUN_LOCK_TTL_SECONDS,
    StateStore,
    StateStoreUnreachable,
)
from .ticker_utils import MAX_WATCHLIST_SIZE, validate_and_dedupe
from .time_gate import MAX_HOLD_SECONDS, DeliveryHold, TimeGate

COMPONENT = "Agent"

EXIT_OK = 0
EXIT_CONFIG_ERROR = 1
#: Every channel refused the alert. Distinct from a config error: the run did
#: its work, but you did not get told. Non-zero so the workflow goes red rather
#: than reporting three green runs that delivered nothing.
EXIT_DELIVERY_FAILED = 2


class Agent:
    def __init__(
        self,
        config: AgentConfig,
        flags: ModeFlags,
        logger: Logger,
        state_store: StateStore,
        time_gate: TimeGate,
        ingestion: IngestionModule,
        news_fetcher: NewsFetcher,
        llm_processor: LLMProcessor,
        cost_guard: CostGuard,
        dedup_filter: DedupFilter,
        alert_builder: AlertBuilder,
        notifier: Notifier,
        backtest_fixture: Optional[BacktestFixture] = None,
        delivery_hold: Optional[DeliveryHold] = None,
    ) -> None:
        self.config = config
        self.flags = flags
        self.logger = logger
        self.store = state_store
        self.time_gate = time_gate
        self.ingestion = ingestion
        self.news = news_fetcher
        self.llm = llm_processor
        self.cost_guard = cost_guard
        self.dedup = dedup_filter
        self.alerts = alert_builder
        self.notifier = notifier
        self.fixture = backtest_fixture
        self.delivery_hold = delivery_hold

    # -- entry point -------------------------------------------------------

    def run(self) -> int:
        started_at = datetime.now(timezone.utc)
        summary = RunSummary(
            run_id=self.logger.run_id,
            mode=self.flags.name,
            severity="info",
            run_start_time=started_at,
            run_end_time=started_at,
        )
        lock = None
        exit_code = EXIT_OK

        try:
            if self.flags.mode is RunMode.TEST_CHANNELS:
                return self._run_test_channels(summary)

            if self.flags.mode is RunMode.LIVE:
                # A --deliver-at hold extends the run by up to MAX_HOLD_SECONDS,
                # so the lock has to outlast it. Without this the lock could
                # expire while we sit waiting for the delivery minute and let a
                # second fire run the whole pipeline alongside this one.
                lock_ttl = RUN_LOCK_TTL_SECONDS + (
                    MAX_HOLD_SECONDS if self.delivery_hold is not None else 0
                )
                lock = self.store.acquire_lock(KEY_RUN_LOCK, lock_ttl)
                if lock is None:
                    self.logger.warning(
                        COMPONENT,
                        "A previous run still holds the run_lock; skipping this fire",
                        holder=_read_lock_holder(self.store),
                        skipped_at=started_at.isoformat(),
                    )
                    return EXIT_OK

            if self.flags.enforce_active_hours and not self.time_gate.in_active_hours(started_at):
                local = self.time_gate.to_local(started_at)
                self.logger.info(
                    COMPONENT,
                    "Run suppressed: outside configured active hours "
                    f"({self.config.active_hours_start}-{self.config.active_hours_end})",
                    local_time=local.isoformat(),
                    utc_time=started_at.isoformat(),
                )
                return EXIT_OK

            exit_code = self._run_pipeline(summary, started_at)
        except StateStoreUnreachable as exc:
            self.logger.error(COMPONENT, str(exc), error_type="StateStoreUnreachable")
            exit_code = EXIT_CONFIG_ERROR
        except MissingAPIKeyError as exc:
            self.logger.error(COMPONENT, str(exc), error_type="MissingCredential")
            exit_code = EXIT_CONFIG_ERROR
        except Exception as exc:  # noqa: BLE001 - never crash without a run summary
            self.logger.error(
                COMPONENT,
                f"Unhandled {type(exc).__name__} during the run: {exc}",
                error_type=type(exc).__name__,
            )
            exit_code = EXIT_OK
        finally:
            if lock is not None:
                self.store.release_lock(lock)
            summary.run_end_time = datetime.now(timezone.utc)
            summary.llm_cost_usd_this_run = self.cost_guard.cost_this_run
            summary.daily_cost_usd_running_total = self.cost_guard.daily_total()
            summary.errors = list(self.logger.collected_errors)
            summary.severity = "error" if summary.errors else "info"
            self.logger.write_run_summary(summary)

        return exit_code

    # -- modes -------------------------------------------------------------

    def _run_test_channels(self, summary: RunSummary) -> int:
        results = self.notifier.test_channels(self.config.channels)
        summary.delivery_channel_statuses = results
        if not results or any(r.status != "success" for r in results):
            return EXIT_CONFIG_ERROR
        return EXIT_OK

    # -- pipeline ----------------------------------------------------------

    def _run_pipeline(self, summary: RunSummary, now: datetime) -> int:
        entries = self._resolve_watchlist()
        if not entries:
            return EXIT_OK

        symbols = [entry.symbol for entry in entries]
        groups = {entry.symbol: entry.group for entry in entries}

        prices, earnings = self._load_market_data(symbols)
        active_symbols = [s for s in symbols if s in prices]
        if not active_symbols:
            self.logger.error(
                COMPONENT,
                "No valid tickers remain after price ingestion; nothing to alert on",
                error_type="NoValidTickers",
            )
            return EXIT_OK
        summary.tickers_processed = active_symbols

        benchmark_prices = prices.get(self.config.benchmark_index.upper())
        events: list[Event] = []
        #: Stories actually sent to the LLM this run, recorded at the end so a
        #: later run does not pay to analyze them again.
        analyzed: list[Story] = []

        for symbol in active_symbols:
            stories = self._load_stories(symbol)
            # Drop already-analyzed stories before the LLM sees them: the 24h
            # lookback overlaps heavily between runs, so this is what stops a
            # later run paying to re-summarize the same news.
            stories = self.dedup.filter_stories(stories)
            if not stories:
                continue
            context = self._price_context(prices[symbol], benchmark_prices, stories)
            try:
                events.extend(self.llm.process(symbol, stories, context, groups.get(symbol)))
            except LLMBudgetExhausted:
                break
            analyzed.extend(stories)

        summary.events_identified = len(events)
        summary.stories_fetched_per_source = dict(self.news.stories_per_source)

        fresh_events = self.dedup.filter(events)
        summary.events_skipped_already_alerted = self.dedup.skipped_already_alerted
        summary.stories_skipped_already_alerted = self.dedup.stories_skipped_already_alerted

        alert = self.alerts.build(
            run_id=self.logger.run_id,
            events=fresh_events,
            tickers_monitored=len(active_symbols),
            earnings=earnings,
            now=now,
            no_events_summary=self.llm.no_events_summary(active_symbols)
            if not fresh_events
            else None,
        )

        suppressed = self._load_suppressed_alert()
        alert = self.alerts.merge(alert, suppressed)

        if not self.flags.may_dispatch:
            print(json.dumps(alert.to_dict(), indent=2))
            self.logger.info(
                COMPONENT,
                f"{self.flags.name}: printed the would-be alert instead of dispatching",
                events=len(alert.events),
            )
            summary.events_dispatched = 0
            return EXIT_OK

        if self.flags.enforce_quiet_hours and self.time_gate.in_quiet_hours(now):
            self._persist_suppressed_alert(alert)
            self.dedup.record_analyzed(analyzed)
            return EXIT_OK

        if not alert.events:
            # Nothing new cleared the threshold: stay quiet rather than send a
            # "nothing happened" message on every scheduled fire.
            self.logger.info(
                COMPONENT,
                "No new events passed the impact threshold; nothing dispatched",
                tickers_monitored=alert.tickers_monitored,
            )
            self.dedup.record_analyzed(analyzed)
            return EXIT_OK

        self._hold_until_delivery_time()

        results = self.notifier.dispatch(alert, self.config.channels)
        summary.delivery_channel_statuses = results

        if any(r.status == "success" for r in results):
            self.dedup.record_dispatched(alert.events)
            self.dedup.record_analyzed(analyzed)
            summary.events_dispatched = len(alert.events)
            if suppressed is not None:
                self.store.delete(KEY_SUPPRESSED_ALERT)
        else:
            # Record nothing: the next run must re-analyze and re-send rather
            # than lose the alert. An agent that cannot deliver is not healthy,
            # so this fails the run loudly instead of exiting green.
            self.logger.error(
                COMPONENT,
                "Every delivery channel failed; nothing was recorded, so the next run retries",
                error_type="AllChannelsFailed",
                channels=[r.channel_type for r in results],
            )
            return EXIT_DELIVERY_FAILED

        return EXIT_OK

    def _hold_until_delivery_time(self) -> None:
        """Wait out the remainder of the gap to --deliver-at, if one is set.

        Reached only on the live dispatch path: --dry-run and --backtest return
        before it, and --test-channels never enters the pipeline at all. A
        diagnostic that made you wait fifteen minutes for its answer would be a
        worse diagnostic.
        """
        if self.delivery_hold is None:
            return

        wait = self.delivery_hold.seconds_until()
        if wait <= 0:
            # The trigger was late enough that the target has already gone by.
            # Send now: the alert is what matters, the minute was the nicety.
            self.logger.info(
                COMPONENT,
                f"Delivery target {self.delivery_hold} already passed "
                f"{abs(wait):.0f}s ago; dispatching immediately",
                target=str(self.delivery_hold),
                seconds_late=round(abs(wait), 1),
            )
            return

        if wait > MAX_HOLD_SECONDS:
            # Too far out to be the intended small gap, so the trigger fired at
            # the wrong time. Holding anyway would burn Actions minutes and let
            # the news age in the queue; deliver it while it is still news.
            self.logger.warning(
                COMPONENT,
                f"Delivery target {self.delivery_hold} is {wait / 60:.0f} min away, "
                f"beyond the {MAX_HOLD_SECONDS // 60} min hold cap; dispatching "
                "immediately. Check when the trigger actually fired.",
                target=str(self.delivery_hold),
                seconds_until_target=round(wait, 1),
            )
            return

        self.logger.info(
            COMPONENT,
            f"Work finished; holding the alert {wait:.0f}s to deliver at "
            f"{self.delivery_hold}",
            target=str(self.delivery_hold),
            hold_seconds=round(wait, 1),
        )
        time.sleep(wait)

    # -- pipeline steps ----------------------------------------------------

    def _resolve_watchlist(self) -> list[TickerEntry]:
        if self.fixture is not None:
            raw = [TickerEntry(symbol=s) for s in self.fixture.tickers]
        else:
            # Loaded and validated by the ConfigLoader before the run started.
            raw = list(self.config.watchlist or [])

        entries, rejected = validate_and_dedupe(raw)
        for symbol in rejected:
            self.logger.warning(
                COMPONENT,
                f"Dropping {symbol!r}: not a valid ticker symbol",
                symbol=symbol,
            )

        if len(entries) > MAX_WATCHLIST_SIZE:
            self.logger.warning(
                COMPONENT,
                f"Watchlist has {len(entries)} tickers; truncating to the first "
                f"{MAX_WATCHLIST_SIZE}",
                watchlist_size=len(entries),
            )
            entries = entries[:MAX_WATCHLIST_SIZE]

        if not entries:
            self.logger.error(
                COMPONENT,
                "Watchlist resolved to zero valid tickers; nothing to do this run",
                error_type="EmptyWatchlist",
            )
        return entries

    def _load_market_data(self, symbols: list[str]):
        if self.fixture is not None:
            return dict(self.fixture.prices), dict(self.fixture.earnings)

        benchmark = self.config.benchmark_index.upper()
        fetch_list = list(symbols)
        if benchmark not in fetch_list:
            fetch_list.append(benchmark)

        prices = self.ingestion.fetch_prices(fetch_list)
        earnings = self.ingestion.fetch_earnings_calendar(symbols)
        return prices, earnings

    def _load_stories(self, symbol: str) -> list[Story]:
        if self.fixture is not None:
            return list(self.fixture.stories.get(symbol, []))
        return self.news.fetch_stories(symbol, self.config.lookback_window_hours)

    def _price_context(
        self,
        ticker_prices: PriceData,
        benchmark_prices: Optional[PriceData],
        stories: list[Story],
    ) -> dict[str, StoryPriceContext]:
        window = self.config.price_window_hours
        context: dict[str, StoryPriceContext] = {}
        for story in stories:
            movement = calculate_price_movement(ticker_prices, story.published_at, window)
            benchmark_move = None
            if benchmark_prices is not None:
                benchmark_move = calculate_price_movement(
                    benchmark_prices, story.published_at, window
                ).value
            context[story.url] = StoryPriceContext(
                movement_pct=movement.value,
                benchmark_movement_pct=benchmark_move,
                pending=movement.pending,
                truncated=movement.truncated,
            )
        return context

    # -- suppressed-alert persistence --------------------------------------

    def _load_suppressed_alert(self) -> Optional[Alert]:
        if not self.flags.may_write_state:
            return None
        try:
            blob = self.store.get(KEY_SUPPRESSED_ALERT)
            if not blob:
                return None
            return Alert.from_dict(json.loads(blob.decode("utf-8")))
        except Exception as exc:  # noqa: BLE001 - a corrupt blob must not kill the run
            self.logger.warning(COMPONENT, f"Could not read the suppressed alert: {exc}")
            return None

    def _persist_suppressed_alert(self, alert: Alert) -> None:
        quiet = self.config.quiet_hours
        try:
            self.store.set(
                KEY_SUPPRESSED_ALERT, json.dumps(alert.to_dict()).encode("utf-8")
            )
        except Exception as exc:  # noqa: BLE001
            self.logger.error(
                COMPONENT,
                f"Could not persist the suppressed alert: {exc}",
                error_type=type(exc).__name__,
            )
        self.logger.info(
            COMPONENT,
            "Within quiet hours "
            f"({quiet.start}-{quiet.end}); alert withheld and persisted for the next active run"
            if quiet
            else "Within quiet hours; alert withheld",
            events=len(alert.events),
        )


def _read_lock_holder(store: StateStore) -> Optional[str]:
    reader = getattr(store, "read_lock_holder", None)
    return reader(KEY_RUN_LOCK) if callable(reader) else None


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


def build_agent(
    config: AgentConfig,
    flags: ModeFlags,
    logger: Logger,
    fixtures_dir: str = "fixtures",
    delivery_hold: Optional[DeliveryHold] = None,
) -> Agent:
    """Construct every component and inject it into the Agent."""
    if config.state_store.type == "memory" and flags.mode is RunMode.LIVE:
        raise StateStoreUnreachable(
            "state_store.type: memory is test-only and cannot be used for a live run "
            "(it would silently disable cross-run deduplication)"
        )

    if flags.mode in (RunMode.TEST_CHANNELS, RunMode.BACKTEST):
        # Neither mode touches cross-run state, and neither should be blocked by
        # an unreachable state store -- that would make the one command for
        # diagnosing a broken setup depend on the rest of it, and would make
        # replaying a canned file require a live Redis.
        #
        # For a backtest this is also a correctness point, not just convenience:
        # reading the production dedup records would make the same fixture
        # produce different output depending on what the live agent happened to
        # have alerted on. A backtest has to be reproducible.
        state_store: StateStore = MemoryStateStore()
    else:
        state_store = build_state_store(config.state_store)
    read_only = not flags.may_write_state

    cost_guard = CostGuard(
        config.cost, config.llm_model, state_store, logger, read_only=read_only
    )
    canned = load_canned_llm_response(fixtures_dir) if not flags.use_llm else None
    fixture = (
        load_backtest_fixture(flags.backtest_date, fixtures_dir) if flags.backtest_date else None
    )

    return Agent(
        config=config,
        flags=flags,
        logger=logger,
        state_store=state_store,
        time_gate=TimeGate(config),
        ingestion=IngestionModule(logger),
        news_fetcher=NewsFetcher(config.news_sources, logger),
        llm_processor=LLMProcessor(config, cost_guard, logger, canned_response=canned),
        cost_guard=cost_guard,
        dedup_filter=DedupFilter(
            state_store,
            logger,
            ttl_hours=config.already_alerted_ttl_hours,
            read_only=not flags.may_dispatch,
        ),
        alert_builder=AlertBuilder(config.timezone),
        notifier=Notifier(logger),
        backtest_fixture=fixture,
        delivery_hold=delivery_hold,
    )
