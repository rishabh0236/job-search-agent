"""Claude provider.

Uses the SDK's native structured output (``messages.parse`` with
``output_format``), so the model is constrained to the requested schema at
decode time rather than asked politely in a prompt. The returned payload is
*still* re-validated by :class:`~packages.core.llm.client.LLMClient` — the guard
against fabricated evidence ids has to run regardless of who parsed the JSON.

Transport retries (429, 5xx, connection resets) are delegated to the SDK, which
honours ``retry-after``. Schema-repair retries belong to the client. Keeping the
two separate stops them from multiplying into a long, expensive backoff chain.
"""

from __future__ import annotations

from typing import Any

from anthropic import (
    Anthropic,
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AuthenticationError,
    RateLimitError,
)
from anthropic.types import ThinkingConfigParam

from packages.core.errors import LLMError, ProviderNotConfigured
from packages.core.llm.base import LLMRequest, LLMUsage, RawCompletion
from packages.core.logging import get_logger
from packages.core.settings import Settings, get_settings

logger = get_logger(__name__)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings | None = None, client: Anthropic | None = None) -> None:
        self._settings = settings or get_settings()
        if client is not None:
            self._client = client
            return

        key = self._settings.anthropic_api_key
        if key is None or not key.get_secret_value():
            raise ProviderNotConfigured(
                "CA_LLM_PROVIDER=anthropic requires CA_ANTHROPIC_API_KEY. "
                "Set it in .env, or use CA_LLM_PROVIDER=stub for offline work."
            )
        self._client = Anthropic(
            api_key=key.get_secret_value(),
            timeout=float(self._settings.llm_timeout_seconds),
            # Transport-level retries only; schema repair is the client's job.
            max_retries=2,
        )

    def complete(
        self,
        request: LLMRequest[Any],
        *,
        repair_hint: str | None = None,
    ) -> RawCompletion:
        user_content = request.render_user_message()
        if repair_hint:
            user_content = f"{user_content}\n\n{repair_hint}"

        kwargs: dict[str, Any] = {
            "model": self._settings.llm_model,
            "max_tokens": request.max_tokens,
            "system": request.full_system(),
            "messages": [{"role": "user", "content": user_content}],
            "output_format": request.output_model,
        }

        # Extended thinking and a pinned temperature are mutually exclusive.
        # Tasks that need reproducibility (parsing, field mapping) pin
        # temperature; reasoning tasks (matching, tailoring) leave it unset and
        # get adaptive thinking, which is where the model's judgement pays off.
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
            disabled: ThinkingConfigParam = {"type": "disabled"}
            kwargs["thinking"] = disabled

        try:
            message = self._client.messages.parse(**kwargs)
        except AuthenticationError as exc:
            raise ProviderNotConfigured(
                "Claude rejected the configured API key", details={"task": request.task}
            ) from exc
        except (RateLimitError, APITimeoutError, APIConnectionError) as exc:
            raise LLMError(
                f"Claude was unreachable for task {request.task!r}: {type(exc).__name__}",
                details={"task": request.task, "retryable": True},
            ) from exc
        except APIStatusError as exc:
            raise LLMError(
                f"Claude returned {exc.status_code} for task {request.task!r}",
                details={"task": request.task, "status": exc.status_code},
            ) from exc

        parsed = message.parsed_output
        if parsed is None:
            # Almost always max_tokens: the model was cut off mid-object.
            raise LLMError(
                f"task {request.task!r} returned no structured output",
                details={"task": request.task, "stop_reason": message.stop_reason},
            )

        return RawCompletion(
            text="",
            parsed=parsed.model_dump(mode="json"),
            model=message.model,
            usage=LLMUsage(
                input_tokens=message.usage.input_tokens,
                output_tokens=message.usage.output_tokens,
            ),
            stop_reason=message.stop_reason,
        )
