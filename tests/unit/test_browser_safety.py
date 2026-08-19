"""Safety detection.

These are the tests that keep the product on the right side of its own rules. Each
one asserts that the agent *stops* — never that it works around anything.
"""

from __future__ import annotations

import pytest

from packages.schemas.enums import StopReason
from services.browser import safety


class TestCaptchaDetection:
    @pytest.mark.parametrize(
        "html",
        [
            '<div class="g-recaptcha" data-sitekey="x"></div>',
            '<div id="captcha-widget" data-captcha="required"></div>',
            "<p>Please complete the CAPTCHA challenge to continue.</p>",
            "<label>I'm not a robot</label>",
            "<script src='https://challenges.cloudflare.com/turnstile/v0/api.js'></script>",
            "<div>Verify you are human before continuing</div>",
            '<div id="cf-challenge-running">Checking your browser</div>',
        ],
    )
    def test_captcha_variants_stop_the_run(self, html: str) -> None:
        verdict = safety.assess(html)
        assert verdict.safe is False
        assert verdict.reason is StopReason.CAPTCHA
        # The explanation must tell the user what to do, not just that it failed.
        assert "yourself" in verdict.detail

    def test_ordinary_form_is_safe(self) -> None:
        html = '<form><input name="full_name"><button>Submit</button></form>'
        assert safety.assess(html).safe is True


class TestAuthDetection:
    @pytest.mark.parametrize(
        "html",
        [
            '<input type="password" name="password">',
            '<input name="otp" placeholder="One-time code">',
            "<p>Please log in to continue</p>",
            "<h1>Single sign-on required</h1>",
        ],
    )
    def test_login_walls_stop_the_run(self, html: str) -> None:
        verdict = safety.assess(html)
        assert verdict.safe is False
        assert verdict.reason is StopReason.UNEXPECTED_AUTH
        assert "never entered automatically" in verdict.detail


class TestPaymentDetection:
    @pytest.mark.parametrize(
        "html",
        [
            "<label>Card number</label><input name='cc_number'>",
            "<p>A $25 application fee is required</p>",
            "<label>CVV</label>",
        ],
    )
    def test_payment_requests_stop_the_run(self, html: str) -> None:
        verdict = safety.assess(html)
        assert verdict.safe is False
        assert verdict.reason is StopReason.PAYMENT_REQUESTED


class TestSuspiciousPages:
    @pytest.mark.parametrize(
        "html",
        [
            "<h1>Access Denied</h1>",
            "<h1>429 Too Many Requests</h1>",
            "<p>We detected unusual traffic from your network</p>",
        ],
    )
    def test_blocks_and_rate_limits_stop_the_run(self, html: str) -> None:
        verdict = safety.assess(html)
        assert verdict.safe is False
        assert verdict.reason is StopReason.SUSPICIOUS_PAGE
        # Explicitly not a retry: pressing against a rate limit is out of bounds.
        assert "rather than retrying" in verdict.detail


class TestPrecedence:
    def test_captcha_wins_over_a_login_prompt(self) -> None:
        """When a page has several, report the hardest stop."""
        html = '<input type="password"><div class="g-recaptcha"></div>'
        assert safety.assess(html).reason is StopReason.CAPTCHA

    def test_payment_wins_over_login(self) -> None:
        html = '<input type="password"><label>Card number</label>'
        assert safety.assess(html).reason is StopReason.PAYMENT_REQUESTED


class TestConfirmation:
    @pytest.mark.parametrize(
        "html",
        [
            "<h1>Application received</h1>",
            "<p>Thank you for applying!</p>",
            "<p>We have received your application.</p>",
        ],
    )
    def test_confirmation_is_recognised(self, html: str) -> None:
        assert safety.looks_like_confirmation(html) is True

    def test_a_plain_form_is_not_a_confirmation(self) -> None:
        assert safety.looks_like_confirmation("<form><input name='x'></form>") is False

    def test_reference_is_extracted_from_a_marked_element(self) -> None:
        html = '<p>Your reference is <strong id="confirmation-ref">MOCK-A1B2C3</strong>.</p>'
        assert safety.extract_confirmation_ref(html) == "MOCK-A1B2C3"

    def test_reference_is_extracted_from_prose(self) -> None:
        html = "<p>Application reference: REQ-99881</p>"
        assert safety.extract_confirmation_ref(html) == "REQ-99881"

    def test_missing_reference_returns_none_rather_than_a_guess(self) -> None:
        """A wrong reference in the tracker is worse than an empty one."""
        assert safety.extract_confirmation_ref("<h1>Application received</h1>") is None


class TestRedaction:
    def test_input_values_are_scrubbed_from_snapshots(self) -> None:
        """A snapshot must not capture the candidate's own answers."""
        html = '<input name="email" value="priya@example.com"><input name="x" value="90000">'
        scrubbed = safety.redact_html(html)
        assert "priya@example.com" not in scrubbed
        assert "90000" not in scrubbed
        assert "[REDACTED]" in scrubbed

    def test_structure_survives_redaction(self) -> None:
        scrubbed = safety.redact_html('<input name="email" value="x">')
        assert 'name="email"' in scrubbed

    def test_output_is_truncated(self) -> None:
        assert len(safety.redact_html("<p>" + "a" * 5000 + "</p>", limit=100)) == 100
