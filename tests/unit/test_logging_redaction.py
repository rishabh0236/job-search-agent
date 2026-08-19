"""Redaction tests.

skills/07 and skills/11 forbid logging passwords, cookies and tokens. Redaction is
enforced in the formatter, so these tests deliberately try to leak via ``extra``.
"""

from __future__ import annotations

import json
import logging

import pytest

from packages.core.logging import (
    REDACTED,
    JsonFormatter,
    SafeLogger,
    get_logger,
    redact,
    safe_extra,
)


def _format(**extra: object) -> dict[str, object]:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="event",
        args=None,
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    parsed: dict[str, object] = json.loads(JsonFormatter().format(record))
    return parsed


class TestRedaction:
    def test_credentials_are_redacted(self) -> None:
        out = _format(password="hunter2", api_key="sk-ant-123", cookie="session=abc")
        assert out["password"] == REDACTED
        assert out["api_key"] == REDACTED
        assert out["cookie"] == REDACTED

    def test_authorization_header_is_redacted_when_nested(self) -> None:
        out = _format(request={"headers": {"Authorization": "Bearer secret-token"}})
        request = out["request"]
        assert isinstance(request, dict)
        headers = request["headers"]
        assert isinstance(headers, dict)
        assert headers["Authorization"] == REDACTED

    def test_candidate_pii_is_redacted_but_ids_are_kept(self) -> None:
        out = _format(email="a@b.com", phone="+1 555 0100", candidate_id="cand_123")
        assert out["email"] == REDACTED
        assert out["phone"] == REDACTED
        # Identifiers must stay loggable or nothing is traceable.
        assert out["candidate_id"] == "cand_123"

    def test_salary_answers_are_redacted(self) -> None:
        out = _format(answers={"expected_salary": "120000"})
        answers = out["answers"]
        assert isinstance(answers, dict)
        assert answers["expected_salary"] == REDACTED

    def test_redaction_reaches_inside_lists(self) -> None:
        result = redact({"items": [{"token": "t1"}, {"safe": "ok"}]})
        assert result == {"items": [{"token": REDACTED}, {"safe": "ok"}]}

    def test_deeply_nested_payloads_are_truncated_not_recursed_forever(self) -> None:
        payload: dict[str, object] = {"level": 0}
        cursor = payload
        for depth in range(1, 12):
            nested: dict[str, object] = {"level": depth}
            cursor["child"] = nested
            cursor = nested
        # Should terminate and mark the cut rather than blow the stack.
        assert "TRUNCATED" in json.dumps(redact(payload))

    def test_message_and_level_survive(self) -> None:
        out = _format()
        assert out["message"] == "event"
        assert out["level"] == "INFO"


class TestSafeExtra:
    """Regression: a reserved key in ``extra`` used to crash the logging call.

    The safety-stop handler logged ``extra={"message": ...}``, which makes stdlib
    logging raise ``KeyError`` — so raising a SafetyStop produced a 500 instead of
    a 409 with an explanation. Exactly the wrong failure mode for the code path
    whose entire job is to hand control back to the user.
    """

    def test_reserved_keys_are_prefixed_not_dropped(self) -> None:
        result = safe_extra({"message": "hi", "args": [1], "reason": "captcha"})
        assert result == {"ctx_message": "hi", "ctx_args": [1], "reason": "captcha"}

    def test_reserved_keys_no_longer_raise_when_logged(self) -> None:
        logger = logging.getLogger("regression.reserved")
        # Would raise KeyError("Attempt to overwrite 'message' in LogRecord").
        logger.info("event", extra=safe_extra({"message": "shadowed", "detail": "ok"}))

    def test_prefixed_values_still_get_redacted(self) -> None:
        out = _format(**safe_extra({"message": "x", "token": "secret"}))
        assert out["token"] == REDACTED


class TestSafeLoggerClass:
    """Regression: `extra={"created": ...}` crashed job discovery.

    Doubly nasty, because it only fires once the level is enabled — the call site
    looked fine until an unrelated test raised the log level, and then real work
    started failing. get_logger() must return a logger immune to this.
    """

    def test_colliding_extra_keys_do_not_raise(self, caplog: pytest.LogCaptureFixture) -> None:
        logger = get_logger("regression.safe_logger")
        with caplog.at_level(logging.INFO, logger="regression.safe_logger"):
            logger.info(
                "jobs.discovered",
                extra={"created": 6, "message": "x", "module": "y", "found": 6},
            )
        assert caplog.records
        record = caplog.records[-1]
        assert record.ctx_created == 6  # type: ignore[attr-defined]
        assert record.found == 6  # type: ignore[attr-defined]

    def test_get_logger_returns_the_safe_class(self) -> None:
        assert isinstance(get_logger("regression.class_check"), SafeLogger)
