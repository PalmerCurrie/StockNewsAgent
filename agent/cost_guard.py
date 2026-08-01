"""LLM spend guardrails (Requirement 12).

Two independent caps:
  * per-run input tokens  -- tracked in memory, resets every invocation
  * per-UTC-day USD spend -- tracked in the StateStore, shared across runs

Both are checked *before* a call against a projection, so the realized total
can overshoot by at most the cost of the one call that tripped the cap. That
slack is deliberate and documented in Property 28.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .logger import Logger
from .models import CostConfig
from .state_store.base import KEY_DAILY_COST_LEDGER, StateStore

COMPONENT = "CostGuard"


class CostGuard:
    def __init__(
        self,
        config: CostConfig,
        model: str,
        state_store: StateStore,
        logger: Logger,
        read_only: bool = False,
    ) -> None:
        self._config = config
        self._model = model
        self._store = state_store
        self._logger = logger
        self._read_only = read_only  # dry-run/backtest must not touch the ledger

        self.tokens_used_this_run = 0
        self.cost_this_run = 0.0

        if model not in config.model_pricing:
            self._logger.warning(
                COMPONENT,
                f"Model {model!r} is absent from cost.model_pricing; estimated_cost_usd will "
                "be 0 and the daily cost cap cannot be enforced",
                model=model,
            )

    # -- ledger ------------------------------------------------------------

    @staticmethod
    def _ledger_key(now: Optional[datetime] = None) -> str:
        day = (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()
        return f"{KEY_DAILY_COST_LEDGER}:{day}"

    def daily_total(self) -> float:
        """Current UTC-day spend. A zero-increment reads it atomically."""
        if self._read_only:
            # Backtest/dry-run must not touch the store at all -- even a
            # zero-increment would create the ledger key.
            return self.cost_this_run
        try:
            return float(self._store.increment(self._ledger_key(), 0.0))
        except Exception as exc:  # noqa: BLE001 - never let bookkeeping kill a run
            self._logger.warning(COMPONENT, f"Could not read the daily cost ledger: {exc}")
            return 0.0

    # -- projections and caps ----------------------------------------------

    def project_cost(self, tokens_in: int, tokens_out_estimate: int, model: Optional[str] = None) -> float:
        pricing = self._config.model_pricing.get(model or self._model)
        if pricing is None:
            return 0.0
        return (
            tokens_in / 1_000_000 * pricing.input_per_1m_usd
            + tokens_out_estimate / 1_000_000 * pricing.output_per_1m_usd
        )

    def would_exceed_cap(self, projected_cost: float) -> bool:
        if projected_cost <= 0:
            # No pricing for this model: the cap is not enforceable (Req 12.4).
            return False
        return self.daily_total() + projected_cost > self._config.daily_cost_cap_usd

    def would_exceed_token_budget(self, tokens_in: int) -> bool:
        return self.tokens_used_this_run + tokens_in > self._config.max_input_tokens_per_run

    # -- recording ---------------------------------------------------------

    def record(self, actual_cost: float, input_tokens: int = 0) -> float:
        self.cost_this_run += actual_cost
        self.tokens_used_this_run += input_tokens
        if self._read_only:
            return self.cost_this_run
        try:
            return float(self._store.increment(self._ledger_key(), actual_cost))
        except Exception as exc:  # noqa: BLE001
            self._logger.warning(COMPONENT, f"Could not update the daily cost ledger: {exc}")
            return self.cost_this_run
