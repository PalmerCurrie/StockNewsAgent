"""LLM provider backends (Requirement 5.2, 5.7).

Both providers constrain the response to the Event JSON schema and report token
usage; everything above this layer is provider-agnostic.

Anthropic is the default. Its structured-output parameter is
``output_config.format`` (the direct analog of OpenAI's
``response_format: json_schema``), and its usage fields are
``input_tokens`` / ``output_tokens`` rather than ``prompt_tokens`` /
``completion_tokens``.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Optional

from .logger import Logger

COMPONENT = "LLMProcessor"

REQUEST_TIMEOUT_SECONDS = 120

# Anthropic structured outputs reject numeric/length constraints in the schema
# (they are enforced in Python by parse_and_validate instead).
_UNSUPPORTED_SCHEMA_KEYS = ("minimum", "maximum", "multipleOf", "minItems", "maxItems", "minLength", "maxLength")

# Claude Opus 5 can decline a request outright; that is an HTTP 200 with
# stop_reason "refusal", not an exception. Server-side refusal fallbacks re-run
# such a request on the recommended model -- only on the tiers that have those
# classifiers in the first place.
ANTHROPIC_FALLBACK_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5", "claude-mythos-5")

# `effort` is an Opus/Sonnet-tier parameter. Sending it to Haiku 4.5 or
# Sonnet 4.5 is an error, not a no-op.
_NO_EFFORT_MODELS = ("claude-haiku", "claude-sonnet-4-5", "claude-3")


def supports_effort(model: str) -> bool:
    return not (model or "").lower().startswith(_NO_EFFORT_MODELS)


def supports_refusal_fallbacks(model: str) -> bool:
    return (model or "").lower().startswith(_FALLBACK_MODELS)


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    refused: bool = False
    refusal_category: Optional[str] = None


class MissingAPIKeyError(Exception):
    """The provider's API key env var is not set -- the Agent exits 1."""


class LLMBackend(ABC):
    provider: str = "unknown"
    api_key_env: str = ""

    @abstractmethod
    def complete(self, prompt: str, schema: dict) -> LLMResponse:
        """Send one prompt and return the schema-constrained JSON text."""

    def require_api_key(self) -> str:
        key = os.environ.get(self.api_key_env, "")
        if not key:
            raise MissingAPIKeyError(
                f"{self.api_key_env} is not set (required for llm_provider: {self.provider})"
            )
        return key


def strip_unsupported_schema_keys(node: Any) -> Any:
    """Recursively drop JSON-Schema keywords Anthropic's structured outputs reject."""
    if isinstance(node, dict):
        return {
            key: strip_unsupported_schema_keys(value)
            for key, value in node.items()
            if key not in _UNSUPPORTED_SCHEMA_KEYS
        }
    if isinstance(node, list):
        return [strip_unsupported_schema_keys(item) for item in node]
    return node


# --------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------


class AnthropicBackend(LLMBackend):
    provider = "anthropic"
    api_key_env = "ANTHROPIC_API_KEY"

    def __init__(
        self,
        model: str,
        logger: Logger,
        max_output_tokens: int = 8000,
        effort: str = "medium",
        use_fallbacks: bool = True,
        client: Any = None,
    ) -> None:
        self._model = model
        self._logger = logger
        self._max_output_tokens = max_output_tokens
        self._client = client
        # Capabilities vary by tier -- sending an unsupported one is a 400.
        self._effort: Optional[str] = effort if supports_effort(model) else None
        self._use_fallbacks = use_fallbacks and supports_refusal_fallbacks(model)
        if effort and self._effort is None:
            logger.info(
                COMPONENT,
                f"llm_effort={effort!r} is ignored: {model} does not support the effort "
                "parameter",
                model=model,
            )

    def _ensure_client(self) -> Any:
        if self._client is None:
            import anthropic  # lazy: keeps the package optional for OpenAI-only runs

            self.require_api_key()
            self._client = anthropic.Anthropic(timeout=REQUEST_TIMEOUT_SECONDS)
        return self._client

    def complete(self, prompt: str, schema: dict) -> LLMResponse:
        client = self._ensure_client()
        output_config: dict[str, Any] = {
            "format": {"type": "json_schema", "schema": strip_unsupported_schema_keys(schema)}
        }
        if self._effort:
            output_config["effort"] = self._effort

        kwargs: dict[str, Any] = {
            "model": self._model,
            # On thinking-capable models max_tokens caps thinking + response
            # text together, so this needs real headroom.
            "max_tokens": self._max_output_tokens,
            "output_config": output_config,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self._use_fallbacks:
            kwargs["betas"] = [ANTHROPIC_FALLBACK_BETA]
            kwargs["fallbacks"] = "default"

        try:
            # Only take the beta endpoint when a beta is actually in use.
            create = client.beta.messages.create if self._use_fallbacks else client.messages.create
            response = create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            # Degrade rather than break every run if a capability guess is wrong
            # or a beta is not enabled for this account.
            if self._use_fallbacks and _mentions(exc, "fallback", "beta"):
                self._logger.warning(
                    COMPONENT,
                    f"Anthropic rejected the {ANTHROPIC_FALLBACK_BETA} beta; "
                    f"retrying without server-side refusal fallbacks ({exc})",
                    model=self._model,
                )
                self._use_fallbacks = False
                return self.complete(prompt, schema)
            if self._effort and _mentions(exc, "effort"):
                self._logger.warning(
                    COMPONENT,
                    f"Model {self._model!r} rejected the effort parameter; retrying without it "
                    f"({exc})",
                    model=self._model,
                )
                self._effort = None
                return self.complete(prompt, schema)
            raise

        usage = getattr(response, "usage", None)
        input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

        # Check stop_reason before reading content: on a refusal, content is
        # empty (pre-output) or partial (mid-stream).
        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            return LLMResponse(
                text="",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                refused=True,
                refusal_category=getattr(details, "category", None) if details else None,
            )

        # With thinking enabled the response carries thinking blocks too; the
        # schema-constrained JSON is in the text block.
        text = next(
            (
                block.text
                for block in getattr(response, "content", [])
                if getattr(block, "type", None) == "text"
            ),
            "",
        )
        return LLMResponse(text=text, input_tokens=input_tokens, output_tokens=output_tokens)


# --------------------------------------------------------------------------
# OpenAI
# --------------------------------------------------------------------------


class OpenAIBackend(LLMBackend):
    provider = "openai"
    api_key_env = "OPENAI_API_KEY"

    def __init__(
        self,
        model: str,
        logger: Logger,
        max_output_tokens: int = 8000,
        client: Any = None,
    ) -> None:
        self._model = model
        self._logger = logger
        self._max_output_tokens = max_output_tokens
        self._client = client
        self._supports_json_schema = True

    def _ensure_client(self) -> Any:
        if self._client is None:
            from openai import OpenAI  # lazy

            self.require_api_key()
            self._client = OpenAI(timeout=REQUEST_TIMEOUT_SECONDS)
        return self._client

    def complete(self, prompt: str, schema: dict) -> LLMResponse:
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "max_completion_tokens": self._max_output_tokens,
        }
        if self._supports_json_schema:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "stock_events", "strict": True, "schema": schema},
            }
        else:
            kwargs["response_format"] = {"type": "json_object"}

        try:
            response = client.chat.completions.create(**kwargs)
        except Exception as exc:  # noqa: BLE001
            if self._supports_json_schema and _mentions(exc, "response_format", "json_schema"):
                self._logger.warning(
                    COMPONENT,
                    f"Model {self._model!r} rejected json_schema response_format; "
                    "falling back to json_object mode",
                    model=self._model,
                )
                self._supports_json_schema = False
                return self.complete(prompt, schema)
            raise

        usage = getattr(response, "usage", None)
        return LLMResponse(
            text=response.choices[0].message.content or "",
            input_tokens=int(getattr(usage, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "completion_tokens", 0) or 0),
        )


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------


def infer_provider(model: str) -> str:
    name = (model or "").lower()
    if name.startswith("claude"):
        return "anthropic"
    if name.startswith(("gpt", "o1", "o3", "o4")):
        return "openai"
    return "anthropic"


def build_backend(config: Any, logger: Logger) -> LLMBackend:
    provider = (config.llm_provider or infer_provider(config.llm_model)).lower()
    if provider == "openai":
        return OpenAIBackend(
            config.llm_model, logger, max_output_tokens=config.llm_max_output_tokens
        )
    return AnthropicBackend(
        config.llm_model,
        logger,
        max_output_tokens=config.llm_max_output_tokens,
        effort=config.llm_effort,
    )


def _mentions(exc: Exception, *needles: str) -> bool:
    text = str(exc).lower()
    return any(needle in text for needle in needles)
