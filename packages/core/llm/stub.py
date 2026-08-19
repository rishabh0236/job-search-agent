"""Deterministic offline provider.

Purpose is not "fake AI" — it is a fixture mechanism. Registered responses make
the whole pipeline runnable and testable with no API key and no network, which is
what lets golden and safety tests assert exact behaviour (skills/09-testing.md).

An unregistered task raises rather than improvising, so a missing fixture is a
loud failure instead of a silently empty result.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pydantic import BaseModel

from packages.core.errors import LLMError
from packages.core.llm.base import LLMRequest, LLMUsage, RawCompletion

#: A fixture is either a literal payload or a function of the request.
StubResponse = dict[str, Any] | BaseModel | Callable[[LLMRequest[Any]], dict[str, Any] | BaseModel]


class StubResponseMissing(LLMError):
    code = "stub_response_missing"


class StubProvider:
    """Serves pre-registered payloads keyed by task name."""

    name = "stub"

    def __init__(self, responses: dict[str, StubResponse] | None = None) -> None:
        self._responses: dict[str, StubResponse] = dict(responses or {})
        #: Every request seen, so tests can assert on prompt construction —
        #: particularly that untrusted content was fenced.
        self.calls: list[LLMRequest[Any]] = []

    def register(self, task: str, response: StubResponse) -> None:
        self._responses[task] = response

    def clear(self) -> None:
        self._responses.clear()
        self.calls.clear()

    def complete(
        self,
        request: LLMRequest[Any],
        *,
        repair_hint: str | None = None,
    ) -> RawCompletion:
        self.calls.append(request)

        if request.task not in self._responses:
            raise StubResponseMissing(
                f"no stub response registered for task {request.task!r}",
                details={
                    "task": request.task,
                    "registered": sorted(self._responses),
                    "hint": "register a fixture via StubProvider.register(task, payload)",
                },
            )

        response = self._responses[request.task]
        if callable(response):
            response = response(request)
        payload = response.model_dump(mode="json") if isinstance(response, BaseModel) else response

        return RawCompletion(
            text="",
            parsed=dict(payload),
            model="stub",
            usage=LLMUsage(input_tokens=0, output_tokens=0),
            stop_reason="end_turn",
        )
