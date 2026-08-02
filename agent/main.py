"""CLI entry point: ``python -m agent.main`` (Requirement 13)."""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from datetime import date
from typing import Optional, Sequence
from zoneinfo import ZoneInfoNotFoundError

from .agent import EXIT_CONFIG_ERROR, build_agent
from .config_loader import ConfigError, ConfigLoader
from .logger import Logger
from .modes import FixtureError, resolve_mode
from .state_store.base import StateStoreUnreachable
from .time_gate import DeliveryHold

COMPONENT = "Main"
DEFAULT_CONFIG_PATH = "config.yaml"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent.main",
        description="Stock News Agent -- scheduled watchlist news and price alerts.",
    )
    parser.add_argument(
        "--config",
        default=os.environ.get("AGENT_CONFIG_PATH", DEFAULT_CONFIG_PATH),
        help=f"path to the YAML/JSON config file (default: {DEFAULT_CONFIG_PATH})",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run the full pipeline but print the would-be alert instead of dispatching",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="with --dry-run/--backtest: substitute the canned LLM response fixture",
    )
    parser.add_argument(
        "--test-channels",
        action="store_true",
        help="send a canned verification message to every configured channel and exit",
    )
    parser.add_argument(
        "--backtest",
        metavar="YYYY-MM-DD",
        help="replay fixtures/backtest/<DATE>.json through the pipeline; never dispatches",
    )
    parser.add_argument(
        "--fixtures-dir",
        default="fixtures",
        help="directory holding canned_llm_response.json and backtest/ (default: fixtures)",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help="local file of KEY=value secrets, loaded if present (default: .env)",
    )
    parser.add_argument(
        "--deliver-at",
        metavar="HH:MM",
        default=os.environ.get("AGENT_DELIVER_AT") or None,
        help="hold a live alert until this local time so it lands on the minute; "
        "the run still fetches at start-up, only the send waits",
    )
    parser.add_argument(
        "--deliver-tz",
        metavar="IANA_ZONE",
        default=os.environ.get("AGENT_DELIVER_TZ") or None,
        help="timezone for --deliver-at, e.g. America/New_York "
        "(default: the config file's timezone)",
    )
    return parser


def load_env_file(path: str) -> Optional[str]:
    """Load a local .env for development.

    Real environment variables always win (``override=False``), so this can
    never shadow a GitHub Actions secret.
    """
    if not os.path.isfile(path):
        return None
    try:
        from dotenv import load_dotenv
    except ImportError:
        print(
            f"{path} exists but python-dotenv is not installed; skipping it "
            "(pip install -r requirements.txt)",
            file=sys.stderr,
        )
        return None
    load_dotenv(path, override=False)
    return path


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    env_file = load_env_file(args.env_file)

    if args.backtest:
        try:
            date.fromisoformat(args.backtest)
        except ValueError:
            print(f"--backtest expects YYYY-MM-DD, got {args.backtest!r}", file=sys.stderr)
            return EXIT_CONFIG_ERROR

    flags = resolve_mode(
        dry_run=args.dry_run,
        no_llm=args.no_llm,
        test_channels=args.test_channels,
        backtest=args.backtest,
    )
    logger = Logger(run_id=str(uuid.uuid4()), mode=flags.name)
    if env_file:
        logger.info(COMPONENT, f"Loaded local secrets from {env_file}", env_file=env_file)

    loader = ConfigLoader()
    try:
        config = loader.load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        logger.error(COMPONENT, str(exc), error_type="ConfigError")
        return EXIT_CONFIG_ERROR

    # Requirement 10.4: report every validation error before touching the network.
    # A backtest brings its own tickers, so it is not held to the watchlist
    # config -- that is what keeps `--backtest --no-llm` runnable on a fresh
    # clone with no credentials of any kind.
    errors = loader.validate(config, requires_watchlist=not flags.backtest_date)
    if errors:
        for message in errors:
            print(f"config error: {message}", file=sys.stderr)
            logger.error(COMPONENT, message, error_type="ConfigValidationError")
        return EXIT_CONFIG_ERROR

    # Requirement 10.4 again: a malformed --deliver-at must fail here, not after
    # the run has spent money and is standing at the dispatch waiting to sleep.
    delivery_hold = None
    if args.deliver_at:
        try:
            delivery_hold = DeliveryHold(args.deliver_at, args.deliver_tz or config.timezone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            message = f"--deliver-at/--deliver-tz is invalid: {exc}"
            print(f"config error: {message}", file=sys.stderr)
            logger.error(COMPONENT, message, error_type="ConfigValidationError")
            return EXIT_CONFIG_ERROR
        logger.info(
            COMPONENT,
            f"Alerts will be held until {delivery_hold}",
            deliver_at=str(delivery_hold),
        )

    try:
        agent = build_agent(config, flags, logger, args.fixtures_dir, delivery_hold)
    except StateStoreUnreachable as exc:
        print(f"state store error: {exc}", file=sys.stderr)
        logger.error(COMPONENT, str(exc), error_type="StateStoreUnreachable")
        return EXIT_CONFIG_ERROR
    except FixtureError as exc:
        print(f"fixture error: {exc}", file=sys.stderr)
        logger.error(COMPONENT, str(exc), error_type="FixtureError")
        return EXIT_CONFIG_ERROR
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        logger.error(COMPONENT, str(exc), error_type="ConfigError")
        return EXIT_CONFIG_ERROR

    try:
        return agent.run()
    finally:
        agent.store.close()


if __name__ == "__main__":
    sys.exit(main())
