"""LLM event extraction with structured output (Requirement 5).

One API call per ticker. The model groups same-event stories, scores impact,
and writes the summary; everything that must be *guaranteed* (word limits,
threshold filtering, URL presence, cost caps) is enforced here in Python
rather than trusted to the prompt.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from .cost_guard import CostGuard
from .llm_backends import LLMBackend, build_backend
from .logger import Logger
from .models import AgentConfig, Event, Story

COMPONENT = "LLMProcessor"

RETRY_DELAY_SECONDS = 5
MAX_SUMMARY_WORDS = 150
MAX_NO_EVENTS_WORDS = 50
OUTPUT_TOKEN_ESTIMATE = 2000  # thinking counts toward output on Claude models

CATEGORY_ENUM = [
    "earnings_release",
    "regulatory_ruling",
    "product_launch",
    "analyst_rating_change",
    "macroeconomic_announcement",
    "other",
]


@dataclass
class StoryPriceContext:
    """Price behaviour in the window following one story's publication."""

    movement_pct: Optional[float] = None
    benchmark_movement_pct: Optional[float] = None
    pending: bool = False
    truncated: bool = False

    @property
    def available(self) -> bool:
        return self.movement_pct is not None and not self.pending


def event_schema(categories: list[str]) -> dict[str, Any]:
    """JSON schema for the structured response.

    Deliberately free of numeric/length constraints (``minimum``, ``minItems``,
    ...): Anthropic's structured outputs reject them, and ``parse_and_validate``
    enforces the same rules in Python for both providers anyway.
    """
    allowed = [c for c in CATEGORY_ENUM if c in categories] or list(categories) or CATEGORY_ENUM
    if "other" not in allowed:
        allowed = allowed + ["other"]
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "events": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "headline": {"type": "string"},
                        "summary": {"type": "string"},
                        "category": {"type": "string", "enum": allowed},
                        "impact_score": {"type": "integer"},
                        "source_urls": {"type": "array", "items": {"type": "string"}},
                        "market_reaction": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "required": [
                        "headline",
                        "summary",
                        "category",
                        "impact_score",
                        "source_urls",
                        "market_reaction",
                    ],
                },
            }
        },
        "required": ["events"],
    }


class LLMBudgetExhausted(Exception):
    """A cost or token cap was hit -- stop calling the LLM for this run."""


class LLMRefused(Exception):
    """The model declined the request -- skip the ticker, do not retry."""


class LLMProcessor:
    def __init__(
        self,
        config: AgentConfig,
        cost_guard: CostGuard,
        logger: Logger,
        backend: Optional[LLMBackend] = None,
        canned_response: Optional[dict] = None,
    ) -> None:
        self._config = config
        self._cost_guard = cost_guard
        self._logger = logger
        self._canned = canned_response
        # Skipped entirely when a canned response stands in for the LLM.
        self._backend = backend or (build_backend(config, logger) if canned_response is None else None)
        self._schema = event_schema(config.high_impact_categories)
        #: Set once a cap trips, so the Agent can stop iterating tickers.
        self.budget_exhausted = False

    # -- public ------------------------------------------------------------

    def process(
        self,
        ticker: str,
        stories: list[Story],
        price_context: Optional[dict[str, StoryPriceContext]] = None,
        group: Optional[str] = None,
    ) -> list[Event]:
        """Return the filtered, price-annotated Events for one ticker."""
        if not stories:
            return []

        price_context = price_context or {}
        prompt = self._build_prompt(ticker, stories, price_context)

        payload = self._request_with_retries(ticker, prompt)
        if payload is None:
            return []

        events = self._to_events(ticker, group, payload, stories, price_context)
        return self._apply_filters(ticker, events)

    def no_events_summary(self, tickers: list[str]) -> str:
        """Requirement 5.6 -- <= 50 words, produced deterministically."""
        listed = ", ".join(tickers[:12])
        if len(tickers) > 12:
            listed += f" and {len(tickers) - 12} more"
        text = (
            f"No significant events for the {len(tickers)} monitored tickers "
            f"({listed}) in this run. Nothing cleared the configured impact threshold of "
            f"{self._config.impact_threshold}."
        )
        return truncate_words(text, MAX_NO_EVENTS_WORDS)

    # -- prompting ---------------------------------------------------------

    def _build_prompt(
        self, ticker: str, stories: list[Story], price_context: dict[str, StoryPriceContext]
    ) -> str:
        threshold = self._config.impact_threshold
        reportable = sorted(self._config.high_impact_categories)
        lines = [
            f"You are a financial news analyst. Analyze the news stories below for {ticker}.",
            "",
            "Instructions:",
            "1. Group stories that describe the same underlying event. When several stories "
            "cover one event, base the summary on the LONGEST story (greatest word count) and "
            "list every unique source URL from the group in source_urls.",
            "2. For each distinct event, assign a category from the allowed enum and an "
            "impact_score from 0 (noise) to 10 (major, market-moving).",
            # The caller drops anything that fails these two tests, so an event
            # that fails them is pure wasted output. Filtering here instead of
            # afterwards is the single largest cost lever in the pipeline.
            f"3. ONLY return events that score {threshold} or higher AND whose category is one "
            f"of: {', '.join(reportable)}. Judge the score first, then write the event out only "
            "if it qualifies. Do not write summaries for events you are going to omit. If "
            "nothing qualifies, return an empty events array -- that is a normal, expected "
            "outcome, so do not lower your standards to fill it.",
            f"4. Write a factual summary of at most {MAX_SUMMARY_WORDS} words per event. Be "
            "concise: cover what happened and why it matters, and stop. Do not speculate and do "
            "not give investment advice.",
            "5. source_urls must be non-empty and must only contain URLs listed below.",
            "6. market_reaction: when price movement is given for a story in the event, write ONE "
            f"sentence characterizing the reaction (positive/negative/muted) versus the "
            f"{self._config.benchmark_index} benchmark over the same window, distinguishing "
            "idiosyncratic from macro-driven movement. When no price movement is given, set "
            "market_reaction to null and say in the summary that price data was unavailable.",
            "",
            f"Allowed categories: {', '.join(self._schema['properties']['events']['items']['properties']['category']['enum'])}",
            f"Price window: {self._config.price_window_hours} hour(s) after publication.",
            "",
            "Stories:",
        ]

        for index, story in enumerate(stories, start=1):
            context = price_context.get(story.url)
            lines.append(
                f"[{index}] headline: {story.headline}\n"
                f"    url: {story.url}\n"
                f"    published_at: {story.published_at.isoformat()}\n"
                f"    source: {story.source} (word_count: {story.word_count})\n"
                f"    price: {_render_price(context, ticker, self._config.benchmark_index)}"
            )

        return "\n".join(lines)

    # -- API calls ---------------------------------------------------------

    def _request_with_retries(self, ticker: str, prompt: str) -> Optional[dict]:
        if self._canned is not None:
            return self._canned

        estimated_input_tokens = max(1, len(prompt) // 4)
        self._check_budget(ticker, estimated_input_tokens)

        raw = self._call_with_transient_retry(ticker, prompt)
        if raw is None:
            return None

        payload, errors = parse_and_validate(raw, self._schema)
        if not errors:
            return payload

        # Requirement 5.10: exactly one validation retry with the error inlined.
        self._logger.warning(
            COMPONENT,
            f"LLM response for {ticker} failed schema validation; retrying once",
            ticker=ticker,
            validation_errors=errors[:5],
        )
        retry_prompt = (
            f"{prompt}\n\nYour previous response was rejected by the schema validator with "
            f"these errors: {'; '.join(errors[:5])}. Return only valid JSON matching the schema."
        )
        self._check_budget(ticker, max(1, len(retry_prompt) // 4))
        raw = self._call_with_transient_retry(ticker, retry_prompt)
        if raw is None:
            return None

        payload, errors = parse_and_validate(raw, self._schema)
        if errors:
            self._logger.error(
                COMPONENT,
                f"LLM response for {ticker} failed schema validation twice; skipping the ticker",
                error_type="SchemaValidationFailed",
                ticker=ticker,
                validation_errors=errors[:5],
            )
            return None
        return payload

    def _call_with_transient_retry(self, ticker: str, prompt: str) -> Optional[str]:
        """Requirement 5.8 -- one retry after 5s, then skip the ticker."""
        for attempt in (1, 2):
            try:
                return self._call_api(prompt, ticker)
            except LLMRefused:
                return None  # already logged; retrying the same prompt is pointless
            except Exception as exc:  # noqa: BLE001 - any API failure is retryable once
                self._logger.llm_call(
                    model=self._config.llm_model,
                    input_tokens=0,
                    output_tokens=0,
                    cost_usd=0.0,
                    severity="warning" if attempt == 1 else "error",
                    ticker=ticker,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
                if attempt == 1:
                    time.sleep(RETRY_DELAY_SECONDS)
                    continue
                self._logger.error(
                    COMPONENT,
                    f"LLM call failed twice for {ticker}; skipping the ticker: {exc}",
                    error_type=type(exc).__name__,
                    ticker=ticker,
                )
                return None
        return None

    def _call_api(self, prompt: str, ticker: str = "") -> str:
        assert self._backend is not None  # canned runs never reach here
        result = self._backend.complete(prompt, self._schema)

        cost = self._cost_guard.project_cost(result.input_tokens, result.output_tokens)
        self._cost_guard.record(cost, input_tokens=result.input_tokens)
        self._logger.llm_call(
            model=self._config.llm_model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            cost_usd=cost,
            severity="warning" if result.refused else "info",
            provider=self._backend.provider,
            ticker=ticker,
            refused=result.refused or None,
        )

        if result.refused:
            # A safety refusal is a content outcome, not a transient error --
            # retrying the same prompt would just burn another call.
            self._logger.warning(
                COMPONENT,
                f"The model declined to process {ticker or 'this ticker'} "
                f"(category={result.refusal_category}); skipping it",
                ticker=ticker,
                refusal_category=result.refusal_category,
            )
            raise LLMRefused(f"refusal: {result.refusal_category}")

        return result.text

    def _check_budget(self, ticker: str, estimated_input_tokens: int) -> None:
        if self._cost_guard.would_exceed_token_budget(estimated_input_tokens):
            self.budget_exhausted = True
            self._logger.warning(
                COMPONENT,
                f"Per-run input token cap reached before processing {ticker}; "
                "dispatching with the events produced so far",
                ticker=ticker,
                tokens_used=self._cost_guard.tokens_used_this_run,
            )
            raise LLMBudgetExhausted("max_input_tokens_per_run reached")

        projected = self._cost_guard.project_cost(estimated_input_tokens, OUTPUT_TOKEN_ESTIMATE)
        if self._cost_guard.would_exceed_cap(projected):
            self.budget_exhausted = True
            self._logger.warning(
                COMPONENT,
                f"Daily cost cap would be exceeded before processing {ticker}; "
                "dispatching with the events produced so far",
                ticker=ticker,
                daily_total_usd=round(self._cost_guard.daily_total(), 6),
                projected_usd=round(projected, 6),
            )
            raise LLMBudgetExhausted("daily_cost_cap_usd reached")

    # -- response -> Events -------------------------------------------------

    def _to_events(
        self,
        ticker: str,
        group: Optional[str],
        payload: dict,
        stories: list[Story],
        price_context: dict[str, StoryPriceContext],
    ) -> list[Event]:
        by_url = {story.url: story for story in stories}
        known_urls = set(by_url)
        events: list[Event] = []

        for raw in payload.get("events", []):
            urls = [u for u in raw.get("source_urls", []) if u in known_urls]
            if not urls:
                # Requirement 5.4: every Event carries at least one real source URL.
                urls = [stories[0].url]

            context = next(
                (price_context[u] for u in urls if price_context.get(u) is not None), None
            )
            published_at = next((by_url[u].published_at for u in urls if u in by_url), None)

            summary = truncate_words(str(raw.get("summary", "")), MAX_SUMMARY_WORDS)
            market_reaction = raw.get("market_reaction")
            if context is None or not context.available:
                # Requirement 5.9 -- no price data, so no market reaction claim.
                market_reaction = None
                if "price data" not in summary.lower():
                    summary = truncate_words(
                        f"{summary} Price data was unavailable for this window.",
                        MAX_SUMMARY_WORDS,
                    )

            events.append(
                Event(
                    ticker=ticker,
                    group=group,
                    headline=str(raw.get("headline", "")).strip(),
                    summary=summary,
                    category=str(raw.get("category", "other")),
                    impact_score=int(raw.get("impact_score", 0)),
                    source_urls=list(dict.fromkeys(urls)),
                    price_movement=context.movement_pct if context else None,
                    price_movement_pending=bool(context.pending) if context else False,
                    price_window_hours=self._config.price_window_hours,
                    market_reaction=market_reaction,
                    published_at=published_at,
                )
            )
        return events

    def _apply_filters(self, ticker: str, events: list[Event]) -> list[Event]:
        """Requirement 5.3 -- threshold AND category must both pass."""
        allowed = set(self._config.high_impact_categories)
        kept: list[Event] = []
        for event in events:
            if event.impact_score < self._config.impact_threshold:
                continue
            if event.category not in allowed:
                continue
            kept.append(event)

        dropped = len(events) - len(kept)
        if dropped:
            self._logger.info(
                COMPONENT,
                f"Filtered out {dropped} low-impact or off-category event(s) for {ticker}",
                ticker=ticker,
                dropped=dropped,
                impact_threshold=self._config.impact_threshold,
            )
        return kept


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def truncate_words(text: str, limit: int) -> str:
    words = (text or "").split()
    if len(words) <= limit:
        return " ".join(words)
    return " ".join(words[:limit]).rstrip(",;:") + "..."


def parse_and_validate(raw: str, schema: dict) -> tuple[dict, list[str]]:
    """Minimal structural validation against the Event schema.

    Deliberately dependency-free: the schema is small and fixed, and the
    failure mode we care about (model returned something else) is caught by
    exactly these checks.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError) as exc:
        return {}, [f"response is not valid JSON: {exc}"]

    if not isinstance(payload, dict):
        return {}, ["top-level value must be a JSON object"]
    if "events" not in payload:
        return payload, ["missing required key 'events'"]
    if not isinstance(payload["events"], list):
        return payload, ["'events' must be an array"]

    allowed_categories = set(
        schema["properties"]["events"]["items"]["properties"]["category"]["enum"]
    )
    errors: list[str] = []
    for index, event in enumerate(payload["events"]):
        prefix = f"events[{index}]"
        if not isinstance(event, dict):
            errors.append(f"{prefix} must be an object")
            continue
        for key in ("headline", "summary", "category", "impact_score", "source_urls"):
            if key not in event:
                errors.append(f"{prefix}.{key} is required")
        if not isinstance(event.get("headline"), str) or not event.get("headline"):
            errors.append(f"{prefix}.headline must be a non-empty string")
        if not isinstance(event.get("summary"), str):
            errors.append(f"{prefix}.summary must be a string")
        if event.get("category") not in allowed_categories:
            errors.append(
                f"{prefix}.category must be one of {sorted(allowed_categories)}, "
                f"got {event.get('category')!r}"
            )
        score = event.get("impact_score")
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 10:
            errors.append(f"{prefix}.impact_score must be an integer 0-10, got {score!r}")
        urls = event.get("source_urls")
        if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
            errors.append(f"{prefix}.source_urls must be a non-empty array of strings")
        reaction = event.get("market_reaction")
        if reaction is not None and not isinstance(reaction, str):
            errors.append(f"{prefix}.market_reaction must be a string or null")

    return payload, errors


def _render_price(
    context: Optional[StoryPriceContext], ticker: str, benchmark: str
) -> str:
    if context is None or context.pending or context.movement_pct is None:
        return "no price movement available for this window"
    text = f"{ticker} moved {context.movement_pct:+.2f}%"
    if context.benchmark_movement_pct is not None:
        text += f", {benchmark} moved {context.benchmark_movement_pct:+.2f}%"
    if context.truncated:
        text += " (window truncated to the last available bar)"
    return text
