"""Structured logging (Requirement 9).

All output is newline-delimited JSON on stdout so GitHub Actions' log viewer
captures it with no extra configuration. ``run_id`` and ``mode`` are bound once
at startup and therefore appear on every entry.
"""

from __future__ import annotations

import sys
from typing import Any, Optional, TextIO

import structlog

from .models import RunSummary

_configured = False


def _rename_level(_logger: Any, _method: str, event_dict: dict) -> dict:
    """structlog calls the field ``level``; Requirement 9.1 calls it ``severity``."""
    if "level" in event_dict:
        event_dict["severity"] = event_dict.pop("level")
    return event_dict


def _configure(stream: TextIO) -> None:
    global _configured
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            _rename_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(default=str),
        ],
        logger_factory=structlog.PrintLoggerFactory(file=stream),
        cache_logger_on_first_use=False,
    )
    _configured = True


class Logger:
    """Thin wrapper over structlog with the field names this project requires."""

    def __init__(self, run_id: str, mode: str, stream: Optional[TextIO] = None) -> None:
        _configure(stream or sys.stdout)
        self.run_id = run_id
        self.mode = mode
        self._log = structlog.get_logger().bind(run_id=run_id, mode=mode)
        # Accumulated for the `errors` array of the run summary (Requirement 9.1).
        self.collected_errors: list[dict] = []

    # -- generic entries ---------------------------------------------------

    def info(self, component: str, message: str, **fields: Any) -> None:
        self._log.info(message, component=component, message=message, **fields)

    def warning(self, component: str, message: str, **fields: Any) -> None:
        self._log.warning(message, component=component, message=message, **fields)

    def error(
        self, component: str, message: str, error_type: str = "Error", **fields: Any
    ) -> None:
        self.collected_errors.append(
            {"component": component, "error_type": error_type, "message": message}
        )
        self._log.error(
            message, component=component, message=message, error_type=error_type, **fields
        )

    # -- specialized entries -----------------------------------------------

    def llm_call(
        self,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float,
        severity: str = "info",
        **fields: Any,
    ) -> None:
        """Requirement 5.7 / 9.3: one entry per LLM call, success or failure."""
        method = getattr(self._log, severity if severity in ("info", "warning", "error") else "info")
        method(
            "llm_call",
            component="LLMProcessor",
            message="llm_call",
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=round(cost_usd, 6),
            **fields,
        )

    def write_run_summary(self, summary: RunSummary) -> None:
        """Requirement 9.1: exactly one run-level entry per invocation."""
        payload = summary.to_dict()
        severity = payload.pop("severity", "info")
        payload.pop("run_id", None)
        payload.pop("mode", None)
        method = getattr(self._log, severity if severity in ("info", "warning", "error") else "info")
        method("run_summary", component="Agent", message="run_summary", **payload)
