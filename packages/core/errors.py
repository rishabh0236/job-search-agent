"""Domain error taxonomy.

Services raise these; the API layer maps them to HTTP status codes in exactly one
place (``apps/api/main.py``). Business logic never imports ``fastapi``.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for all expected, non-bug failures."""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    """The request contradicts current state (e.g. duplicate application)."""

    code = "conflict"
    http_status = 409


class ValidationFailed(DomainError):
    code = "validation_failed"
    http_status = 422


class EvidenceMissing(DomainError):
    """A claim was asserted without a resolvable evidence reference.

    This is the enforcement point for the product's core promise: nothing reaches
    a resume or an application answer unless it traces back to candidate evidence.
    """

    code = "evidence_missing"
    http_status = 422


class LLMError(DomainError):
    """The model call failed, or its output never satisfied the schema."""

    code = "llm_error"
    http_status = 502


class ProviderNotConfigured(DomainError):
    code = "provider_not_configured"
    http_status = 503


class SafetyStop(DomainError):
    """A guarded workflow refused to continue and needs human intervention.

    Raised on CAPTCHA, unexpected authentication, suspicious pages, payment
    requests, or unknown high-impact questions. Carries the reason so the UI can
    explain precisely what happened and what the user should do.
    """

    code = "safety_stop"
    http_status = 409

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, details=details)
        self.reason = reason


class CompilationFailed(DomainError):
    """LaTeX did not compile; the tailored resume must not be used."""

    code = "compilation_failed"
    http_status = 422
