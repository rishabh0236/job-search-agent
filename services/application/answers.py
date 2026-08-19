"""Mapping candidate facts onto application form fields (FR-41/FR-42).

Three tiers, in order of trust:

1. **Deterministic mapping.** Name, email, phone and location come straight off
   verified contact facts. No model, no ambiguity.
2. **Sensitive fields.** Salary, work authorization, visa status, notice period,
   clearance and demographics *always* go to the user, even when a fact looks like
   it answers them. These are the questions where a wrong answer is not an
   embarrassment but a misrepresentation on a legal document.
3. **Everything else.** Offered to the model, which must either answer with cited
   evidence or say it cannot. "I cannot answer this" is a first-class outcome.

Nothing here writes to a form. It produces answers for review.
"""

from __future__ import annotations

import re

from packages.core.db.models import CandidateFact, Evidence
from packages.core.ids import new_id
from packages.schemas.application import ApplicationAnswer, FormField
from packages.schemas.enums import FactCategory, Provenance

#: Field-name/label patterns that always require the human, regardless of facts.
SENSITIVE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"salary|compensation|pay|remuneration|ctc|expected_?(?:pay|salary)", re.I),
    re.compile(r"work[_\s-]?auth|authoriz|authoris|right[_\s-]?to[_\s-]?work", re.I),
    re.compile(r"visa|sponsor|immigration|permit", re.I),
    re.compile(r"clearance|security[_\s-]?check|background[_\s-]?check", re.I),
    re.compile(r"notice[_\s-]?period|availability|start[_\s-]?date", re.I),
    re.compile(r"disab|veteran|ethnic|race|gender|sexual|religio|marital|age|birth", re.I),
    re.compile(r"criminal|conviction|felony", re.I),
    re.compile(r"reference|referee|referral", re.I),
    re.compile(r"current[_\s-]?employer|notice|resign", re.I),
)

#: Deterministic mappings: contact attribute kind -> field-name patterns.
_CONTACT_MAP: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"e-?mail", re.I)),
    ("phone", re.compile(r"phone|mobile|tel|contact[_\s-]?number", re.I)),
    ("linkedin", re.compile(r"linked-?in", re.I)),
    ("github", re.compile(r"git-?hub", re.I)),
)

_NAME_RE = re.compile(r"^(?:full[_\s-]?)?name$|first[_\s-]?name|last[_\s-]?name|surname", re.I)
_LOCATION_RE = re.compile(r"location|city|address|based|residen", re.I)
_TERMS_RE = re.compile(r"terms|consent|agree|accurate|privacy|gdpr", re.I)


def is_sensitive(field: FormField) -> bool:
    """True when a field must be answered by the human."""
    haystack = f"{field.name} {field.label}"
    return any(pattern.search(haystack) for pattern in SENSITIVE_PATTERNS)


def _verified(facts: list[CandidateFact]) -> list[CandidateFact]:
    """Only verified, non-unknown facts may auto-fill a form."""
    return [
        fact
        for fact in facts
        if fact.verified and fact.provenance in (Provenance.RESUME, Provenance.USER)
    ]


def _contact_value(facts: list[CandidateFact], kind: str) -> tuple[str, CandidateFact] | None:
    for fact in facts:
        if fact.category is FactCategory.CONTACT and fact.attributes.get("kind") == kind:
            return str(fact.attributes.get("value") or fact.claim), fact
    return None


def _identity_name(facts: list[CandidateFact], display_name: str | None) -> str | None:
    for fact in facts:
        if fact.category is FactCategory.IDENTITY:
            return fact.claim
    return display_name


def map_deterministic(
    fields: list[FormField],
    facts: list[CandidateFact],
    *,
    application_id: str,
    display_name: str | None = None,
) -> tuple[list[ApplicationAnswer], list[FormField]]:
    """Fill what code can fill. Returns ``(answers, fields_still_open)``.

    A deterministic answer is marked ``user_verified`` because it is a copy of a fact
    the user already confirmed — it is their own email address, not a suggestion.
    """
    usable = _verified(facts)
    answers: list[ApplicationAnswer] = []
    remaining: list[FormField] = []

    for field in fields:
        if field.field_type == "file":
            # The resume attachment is handled by the runner, which uses the exact
            # approved artifact rather than picking a file.
            continue

        if is_sensitive(field):
            answers.append(
                ApplicationAnswer(
                    id=new_id("application_answer"),
                    application_id=application_id,
                    field=field.name,
                    question=field.label or field.name,
                    answer="",
                    source=Provenance.UNKNOWN,
                    confidence=0.0,
                    user_verified=False,
                    sensitive=True,
                )
            )
            continue

        resolved: tuple[str, CandidateFact | None] | None = None

        if _NAME_RE.search(field.name) or _NAME_RE.search(field.label):
            name = _identity_name(usable, display_name)
            if name:
                resolved = (name, None)
        elif _TERMS_RE.search(f"{field.name} {field.label}") and field.field_type == "checkbox":
            # Confirming one's own information is accurate is the candidate's
            # statement to make, so it is proposed but still needs their tick.
            answers.append(
                ApplicationAnswer(
                    id=new_id("application_answer"),
                    application_id=application_id,
                    field=field.name,
                    question=field.label or field.name,
                    answer="yes",
                    source=Provenance.AI,
                    confidence=0.5,
                    user_verified=False,
                    sensitive=False,
                )
            )
            continue
        else:
            for kind, pattern in _CONTACT_MAP:
                if pattern.search(field.name) or pattern.search(field.label):
                    found = _contact_value(usable, kind)
                    if found is not None:
                        resolved = (found[0], found[1])
                    break
            else:
                if _LOCATION_RE.search(f"{field.name} {field.label}"):
                    found = _contact_value(usable, "location")
                    if found is not None:
                        resolved = (found[0], found[1])

        if resolved is None:
            remaining.append(field)
            continue

        value, fact = resolved
        answers.append(
            ApplicationAnswer(
                id=new_id("application_answer"),
                application_id=application_id,
                field=field.name,
                question=field.label or field.name,
                answer=value,
                source=Provenance.RESUME if fact is not None else Provenance.USER,
                confidence=1.0,
                user_verified=True,
                sensitive=False,
            )
        )

    return answers, remaining


def format_fields_for_prompt(fields: list[FormField]) -> str:
    lines: list[str] = []
    for field in fields:
        parts = [f"- name={field.name}", f"type={field.field_type}"]
        if field.label:
            parts.append(f'label="{field.label}"')
        if field.required:
            parts.append("required")
        if field.options:
            parts.append(f"options={field.options}")
        lines.append(" ".join(parts))
    return "\n".join(lines)


def format_facts_for_prompt(facts: list[CandidateFact], evidence: dict[str, Evidence]) -> str:
    lines: list[str] = []
    for fact in _verified(facts):
        ids = [record.id for record in fact.evidence if record.id in evidence]
        citation = f" [evidence: {', '.join(ids)}]" if ids else " [evidence: none]"
        lines.append(f"- ({fact.category.value}) {fact.claim}{citation}")
    return "\n".join(lines)


def validate_answer(field: FormField, answer: str) -> str | None:
    """Check an answer against the field's own constraints.

    Returns an error message, or None when the answer is usable. This runs on model
    output before it can be shown as ready: a select field answered with prose would
    fail silently in the browser and look like an automation bug.
    """
    if not answer.strip():
        return "answer is empty" if field.required else None

    if field.options and answer not in field.options:
        return f"answer must be one of {field.options}"

    if field.field_type == "number" and not re.fullmatch(r"\d+", answer.strip()):
        return "field expects digits only"

    if field.field_type == "email" and not re.fullmatch(
        r"[^@\s]+@[^@\s]+\.[^@\s]+", answer.strip()
    ):
        return "field expects an email address"

    if field.max_length is not None and len(answer) > field.max_length:
        return f"answer exceeds the field limit of {field.max_length} characters"

    #: A model filling a required field with a placeholder is worse than leaving it
    #: for the user, because it looks answered.
    if answer.strip().lower() in ("n/a", "na", "none", "tbd", "unknown", "-", "xxx"):
        return "placeholder answers are not acceptable; the user must supply this"

    return None
