"""Deterministic detection of states where automation must stop.

Pure functions over page HTML, deliberately: this is the code that decides whether
the agent keeps going or hands control back, and it must be testable without a
browser, reproducible, and impossible to talk out of a stop by an LLM.

The detectors are intentionally **eager**. A false stop costs the user a click; a
missed CAPTCHA means software attempting to work around an anti-bot control, which is
a line this product does not cross (CLAUDE.md rule 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from packages.schemas.enums import StopReason

_CAPTCHA_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"recaptcha|g-recaptcha|hcaptcha|h-captcha|turnstile|funcaptcha", re.I),
    re.compile(r"data-captcha|captcha-widget|captcha_challenge", re.I),
    re.compile(r"\bcaptcha\b", re.I),
    re.compile(r"i'?m not a robot|verify (?:you are|you're) human|human verification", re.I),
    re.compile(r"cf-challenge|challenge-platform|__cf_chl", re.I),
)

_LOGIN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'type=["\']password["\']', re.I),
    re.compile(r"\b(?:sign in|log in|login)\b[^<]{0,40}(?:to continue|required)", re.I),
    re.compile(r"name=[\"'](?:password|passwd|otp|mfa|two_factor)[\"']", re.I),
    re.compile(r"single sign-?on|saml|oauth consent", re.I),
)

_PAYMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"card number|cvv|cvc|expiry date|billing address", re.I),
    re.compile(r"name=[\"'](?:card|cc|credit_card|payment)", re.I),
    re.compile(r"application fee|pay to apply|payment required", re.I),
)

#: Pages that are not what we expected to be on at all.
_SUSPICIOUS_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"access denied|forbidden|rate limit|too many requests", re.I),
    re.compile(r"unusual (?:traffic|activity)|automated (?:traffic|access)", re.I),
    re.compile(r"your (?:ip|account) has been (?:blocked|flagged)", re.I),
)

#: How a confirmation is usually phrased, plus the reference beside it.
_CONFIRMATION_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"application (?:received|submitted|complete)", re.I),
    re.compile(r"thank you for (?:your )?appl", re.I),
    re.compile(r"we(?:'ve| have) received your application", re.I),
)
#: The keyword is matched case-insensitively, but the reference itself is NOT: with a
#: global IGNORECASE the uppercase class matched ordinary prose, so "Application
#: received" yielded the reference "received". A separator is also required, so a
#: sentence merely containing the word "reference" cannot produce one.
_REFERENCE_RE = re.compile(
    r"(?i:\b(?:reference|confirmation)\s*(?:id|number|no|ref|#)?)\s*[:#]\s*"
    r"([A-Z0-9][A-Z0-9-]{3,24})\b"
)
_REFERENCE_ELEMENT_RE = re.compile(
    r'id=["\'](?:confirmation-ref|application-ref|reference)["\'][^>]*>\s*([^<]{3,40})', re.I
)


@dataclass(slots=True)
class SafetyVerdict:
    """What a page means for the run."""

    safe: bool
    reason: StopReason | None = None
    detail: str = ""
    matched: list[str] = field(default_factory=list)

    @classmethod
    def ok(cls) -> SafetyVerdict:
        return cls(safe=True)


def _first_match(html: str, patterns: tuple[re.Pattern[str], ...]) -> str | None:
    for pattern in patterns:
        found = pattern.search(html)
        if found is not None:
            return found.group(0)[:120]
    return None


def assess(html: str, *, url: str = "") -> SafetyVerdict:
    """Classify a page. Order matters: the hardest stop wins."""
    captcha = _first_match(html, _CAPTCHA_PATTERNS)
    if captcha is not None:
        return SafetyVerdict(
            safe=False,
            reason=StopReason.CAPTCHA,
            detail=(
                "The page presents a CAPTCHA or bot-verification challenge. "
                "Automation stops here by design — please complete it yourself and "
                "then continue the run."
            ),
            matched=[captcha],
        )

    payment = _first_match(html, _PAYMENT_PATTERNS)
    if payment is not None:
        return SafetyVerdict(
            safe=False,
            reason=StopReason.PAYMENT_REQUESTED,
            detail=(
                "The page asks for payment or card details. Nothing was entered; "
                "review this application manually."
            ),
            matched=[payment],
        )

    login = _first_match(html, _LOGIN_PATTERNS)
    if login is not None:
        return SafetyVerdict(
            safe=False,
            reason=StopReason.UNEXPECTED_AUTH,
            detail=(
                "The page requires signing in. Credentials are never entered "
                "automatically — please authenticate yourself and continue."
            ),
            matched=[login],
        )

    suspicious = _first_match(html, _SUSPICIOUS_PATTERNS)
    if suspicious is not None:
        return SafetyVerdict(
            safe=False,
            reason=StopReason.SUSPICIOUS_PAGE,
            detail=(
                "The site returned an access-denied or rate-limit page. Stopping "
                "rather than retrying, so the run does not press against a control."
            ),
            matched=[suspicious],
        )

    return SafetyVerdict.ok()


def looks_like_confirmation(html: str) -> bool:
    return any(pattern.search(html) for pattern in _CONFIRMATION_PATTERNS)


def extract_confirmation_ref(html: str) -> str | None:
    """Pull a confirmation reference off a success page.

    Tries a marked element first, then prose. Returns None rather than a guess: a
    wrong reference in the tracker is worse than an empty one.
    """
    element = _REFERENCE_ELEMENT_RE.search(html)
    if element is not None:
        candidate = element.group(1).strip()
        if candidate:
            return candidate[:40]

    prose = _REFERENCE_RE.search(html)
    if prose is not None:
        return prose.group(1).strip()[:40]
    return None


def redact_html(html: str, limit: int = 2000) -> str:
    """Trim and scrub page HTML before it is logged or stored as a snapshot.

    Input values are stripped: a snapshot taken mid-run would otherwise capture the
    candidate's own answers, which is exactly the data we promise not to log.
    """
    scrubbed = re.sub(r'(value=)(["\'])(?:(?!\2).){1,400}\2', r"\1\2[REDACTED]\2", html)
    scrubbed = re.sub(
        r"(<input[^>]*type=[\"']password[\"'][^>]*)value=[^\s>]*", r"\1", scrubbed, flags=re.I
    )
    return scrubbed[:limit]
