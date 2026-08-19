"""The single entry point for model calls.

Responsibilities kept here so no service has to remember them:
* schema validation with a bounded repair loop;
* the evidence-id allowlist guard;
* usage/attempt logging that never records prompt or candidate content;
* provider selection from settings.
"""

from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from packages.core.errors import LLMError
from packages.core.llm.base import LLMProvider, LLMRequest, LLMResult
from packages.core.llm.guards import assert_evidence_allowed
from packages.core.logging import get_logger
from packages.core.settings import Settings, get_settings

logger = get_logger(__name__)

T = TypeVar("T", bound=BaseModel)


def _extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object from model text, tolerating fenced code blocks."""
    candidate = text.strip()
    if candidate.startswith("```"):
        lines = [line for line in candidate.splitlines() if not line.strip().startswith("```")]
        candidate = "\n".join(lines).strip()
    # Fall back to the outermost braces if the model added prose around the JSON.
    if not candidate.startswith("{"):
        start, end = candidate.find("{"), candidate.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found in model output")
        candidate = candidate[start : end + 1]
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


class LLMClient:
    """Validating wrapper around an :class:`LLMProvider`."""

    def __init__(self, provider: LLMProvider, settings: Settings | None = None) -> None:
        self._provider = provider
        self._settings = settings or get_settings()

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def run(self, request: LLMRequest[T]) -> LLMResult[T]:
        """Execute ``request``, returning validated output.

        Retries only on *schema* failures: the provider is responsible for
        retrying transport errors. Raises :class:`LLMError` when the model cannot
        produce conforming output within the retry budget.
        """
        max_attempts = max(1, self._settings.llm_max_retries + 1)
        repair_hint: str | None = None
        last_error: str = ""

        for attempt in range(1, max_attempts + 1):
            completion = self._provider.complete(request, repair_hint=repair_hint)
            try:
                payload = (
                    completion.parsed
                    if completion.parsed is not None
                    else _extract_json(completion.text)
                )
                output = request.output_model.model_validate(payload)
            except (ValidationError, ValueError, json.JSONDecodeError) as exc:
                last_error = str(exc)[:1500]
                repair_hint = (
                    "Your previous response did not satisfy the required schema. "
                    f"Fix exactly these problems and return only valid JSON:\n{last_error}"
                )
                logger.warning(
                    "llm.schema_invalid",
                    extra={
                        "task": request.task,
                        "attempt": attempt,
                        "provider": self._provider.name,
                        "error_kind": type(exc).__name__,
                    },
                )
                continue

            if request.allowed_evidence_ids is not None:
                # Raises EvidenceMissing — deliberately not retried: a fabricated
                # citation is a correctness failure to surface, not a formatting
                # slip to coax the model out of.
                assert_evidence_allowed(output, request.allowed_evidence_ids, task=request.task)

            logger.info(
                "llm.ok",
                extra={
                    "task": request.task,
                    "provider": self._provider.name,
                    "model": completion.model,
                    "attempts": attempt,
                    "input_tokens": completion.usage.input_tokens,
                    "output_tokens": completion.usage.output_tokens,
                },
            )
            return LLMResult(
                output=output,
                task=request.task,
                model=completion.model,
                provider=self._provider.name,
                attempts=attempt,
                usage=completion.usage,
            )

        raise LLMError(
            f"task {request.task!r} produced no schema-valid output after {max_attempts} attempts",
            details={"last_error": last_error, "provider": self._provider.name},
        )


def build_client(settings: Settings | None = None) -> LLMClient:
    """Construct the client for the configured provider.

    Imports are local so that selecting the stub provider never requires the
    ``anthropic`` package to be importable, and vice versa.
    """
    settings = settings or get_settings()

    if settings.llm_provider == "anthropic":
        from packages.core.llm.anthropic_provider import AnthropicProvider

        return LLMClient(AnthropicProvider(settings), settings)

    from packages.core.llm.stub import StubProvider

    return LLMClient(StubProvider(), settings)
