"""Structured JSON logging with mandatory secret redaction.

The skills file is explicit: never log passwords, cookies, tokens or sensitive
application answers. Redaction lives in the formatter rather than at call sites,
so a careless ``logger.info(..., extra={"headers": headers})`` still cannot leak
a credential.
"""

from __future__ import annotations

import json
import logging
import sys
from collections.abc import Mapping
from typing import Any

REDACTED = "[REDACTED]"

# Substring match against the *key*, lowercased. Deliberately broad.
SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "password",
    "passwd",
    "secret",
    "token",
    "api_key",
    "apikey",
    "authorization",
    "auth",
    "cookie",
    "session_id",
    "credential",
    "ssn",
    "salary",
)

# Keys that carry candidate PII we do not want in logs even though they are not
# credentials. Keep identifiers (ids) loggable so events remain traceable.
PII_KEY_PARTS: tuple[str, ...] = ("email", "phone", "address", "date_of_birth")

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def safe_extra(fields: dict[str, Any]) -> dict[str, Any]:
    """Make a dict safe to pass as ``logging`` ``extra=``.

    The stdlib raises ``KeyError`` if ``extra`` contains a key that collides with
    a ``LogRecord`` attribute — ``message``, ``args``, ``name``, ``module`` and
    friends. That turns an innocuous log line into a runtime crash at exactly the
    moment you were trying to record something interesting, so colliding keys are
    prefixed rather than dropped.
    """
    return {(f"ctx_{key}" if key in _RESERVED else key): value for key, value in fields.items()}


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS + PII_KEY_PARTS)


def redact(value: Any, _depth: int = 0) -> Any:
    """Recursively redact sensitive values in a log payload."""
    if _depth > 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            key: (REDACTED if _is_sensitive(str(key)) else redact(val, _depth + 1))
            for key, val in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item, _depth + 1) for item in value]
    return value


class JsonFormatter(logging.Formatter):
    """One JSON object per line — greppable locally, ingestible later."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if extras:
            payload.update(redact(extras))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class SafeLogger(logging.Logger):
    """A logger that cannot be crashed by a colliding ``extra`` key.

    ``Logger.makeRecord`` raises ``KeyError`` when ``extra`` contains a key that
    shadows a ``LogRecord`` attribute — ``created``, ``message``, ``module``,
    ``args``, ``name``, ``process`` and a dozen more. The failure is doubly nasty:

    * it turns a log line into an exception that aborts real work, and
    * it only fires when the level is enabled, so a call site logging at INFO looks
      fine until something raises the log level, at which point unrelated code
      starts failing.

    Both of those bit this codebase (``message`` in the safety-stop handler,
    ``created`` in job discovery). Sanitising inside ``makeRecord`` fixes every call
    site at once, including ones not written yet.
    """

    def makeRecord(  # the signature is fixed by the stdlib
        self,
        name: str,
        level: int,
        fn: str,
        lno: int,
        msg: object,
        args: Any,
        exc_info: Any,
        func: str | None = None,
        extra: Mapping[str, Any] | None = None,
        sinfo: str | None = None,
    ) -> logging.LogRecord:
        return super().makeRecord(
            name,
            level,
            fn,
            lno,
            msg,
            args,
            exc_info,
            func,
            safe_extra(dict(extra)) if extra else extra,
            sinfo,
        )


# Installed at import time, not inside configure_logging: modules create their
# logger at import (``logger = get_logger(__name__)``), which happens long before
# any configuration call, and setLoggerClass only affects loggers created after it.
logging.setLoggerClass(SafeLogger)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON formatter on the root logger. Idempotent."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())

    # uvicorn installs its own colourised handlers; route them through ours.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
