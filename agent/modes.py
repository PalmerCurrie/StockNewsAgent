"""Operational modes (Requirement 13).

Each mode is expressed as a set of capability flags rather than as ``if mode ==``
checks scattered through the orchestrator, so the non-mutation guarantees
(dry-run never dispatches; backtest never writes state) hold in one place.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

from .models import PriceData, RunMode, Story

DEFAULT_FIXTURES_DIR = "fixtures"
CANNED_LLM_RESPONSE_FILENAME = "canned_llm_response.json"


@dataclass(frozen=True)
class ModeFlags:
    mode: RunMode
    use_llm: bool = True
    may_dispatch: bool = True
    may_write_state: bool = True
    enforce_active_hours: bool = True
    enforce_quiet_hours: bool = True
    backtest_date: Optional[str] = None

    @property
    def name(self) -> str:
        return self.mode.value


def resolve_mode(
    dry_run: bool = False, no_llm: bool = False, test_channels: bool = False,
    backtest: Optional[str] = None,
) -> ModeFlags:
    """Map CLI flags to capabilities. Precedence: backtest > test-channels > dry-run."""
    if backtest:
        return ModeFlags(
            mode=RunMode.BACKTEST,
            use_llm=not no_llm,
            may_dispatch=False,
            may_write_state=False,
            enforce_active_hours=False,
            enforce_quiet_hours=False,
            backtest_date=backtest,
        )
    if test_channels:
        return ModeFlags(
            mode=RunMode.TEST_CHANNELS,
            use_llm=False,
            may_dispatch=True,
            may_write_state=False,
            enforce_active_hours=False,
            enforce_quiet_hours=False,
        )
    if dry_run:
        return ModeFlags(
            mode=RunMode.DRY_RUN,
            use_llm=not no_llm,
            may_dispatch=False,
            # --dry-run --no-llm writes nothing at all; a plain --dry-run still
            # avoids already_alerted but may touch the cost ledger it incurs.
            may_write_state=not no_llm,
            enforce_active_hours=False,
            enforce_quiet_hours=False,
        )
    return ModeFlags(mode=RunMode.LIVE)


class FixtureError(Exception):
    """A required fixture file is missing or malformed."""


def load_canned_llm_response(fixtures_dir: str = DEFAULT_FIXTURES_DIR) -> dict:
    path = os.path.join(fixtures_dir, CANNED_LLM_RESPONSE_FILENAME)
    if not os.path.isfile(path):
        raise FixtureError(f"--no-llm requires a canned response at {path}")
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or "events" not in payload:
        raise FixtureError(f"{path} must be an object with an 'events' array")
    return payload


@dataclass
class BacktestFixture:
    """A frozen day of inputs: tickers, their stories, prices, earnings dates."""

    tickers: list[str]
    stories: dict[str, list[Story]]
    prices: dict[str, PriceData]
    earnings: dict[str, Optional[date]]


def load_backtest_fixture(
    day: str, fixtures_dir: str = DEFAULT_FIXTURES_DIR
) -> BacktestFixture:
    path = os.path.join(fixtures_dir, "backtest", f"{day}.json")
    if not os.path.isfile(path):
        raise FixtureError(f"No backtest fixture at {path}")
    with open(path, "r", encoding="utf-8") as handle:
        raw: dict[str, Any] = json.load(handle)

    stories = {
        ticker: [Story.from_dict(item) for item in items]
        for ticker, items in (raw.get("stories") or {}).items()
    }
    prices = {
        ticker: PriceData.from_dict(payload)
        for ticker, payload in (raw.get("prices") or {}).items()
    }
    earnings = {
        ticker: (date.fromisoformat(value) if value else None)
        for ticker, value in (raw.get("earnings") or {}).items()
    }
    tickers = raw.get("tickers") or sorted(set(stories) | set(prices))
    if not tickers:
        raise FixtureError(f"{path} contains no tickers")

    return BacktestFixture(tickers=tickers, stories=stories, prices=prices, earnings=earnings)
