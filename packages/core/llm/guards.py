"""Output guards applied to every LLM proposal before it can be persisted.

These are the mechanical enforcement of the product's central promise. A model
that hallucinates gets rejected here rather than talked out of it in a prompt.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from packages.core.errors import EvidenceMissing

#: Keys anywhere in an LLM payload that hold an evidence identifier.
EVIDENCE_KEYS = ("evidence_id", "evidence_ids", "evidence_refs")

#: Patterns that indicate the model injected a fabricated quantitative claim.
#: Used by the tailoring validator, which additionally checks the source text.
METRIC_PATTERN = re.compile(
    r"""(?ix)
    (?:
        \b\d+(?:\.\d+)?\s*%            # 40%, 12.5 %
      | \b(?:\$|usd|eur|inr|£|€)\s?\d  # $2M, EUR 30k
      | \b\d+(?:\.\d+)?\s*x\b          # 3x, 2.5x
      | \b\d+\s*(?:k|m|bn|billion|million|thousand)\b
      | \b\d+\+?\s*years?\b            # 5 years, 10+ years
    )
    """
)


def collect_evidence_ids(payload: Any, _depth: int = 0) -> set[str]:
    """Recursively gather every evidence id referenced in an LLM payload."""
    found: set[str] = set()
    if _depth > 12:
        return found
    if isinstance(payload, BaseModel):
        return collect_evidence_ids(payload.model_dump(), _depth + 1)
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in EVIDENCE_KEYS:
                if isinstance(value, str):
                    found.add(value)
                elif isinstance(value, list):
                    found.update(str(item) for item in value if isinstance(item, (str, int)))
                    # A list of dicts (full EvidenceRef objects) still recurses below.
            found |= collect_evidence_ids(value, _depth + 1)
        return found
    if isinstance(payload, (list, tuple)):
        for item in payload:
            found |= collect_evidence_ids(item, _depth + 1)
    return found


def assert_evidence_allowed(payload: Any, allowed: frozenset[str], *, task: str) -> None:
    """Reject output citing evidence the model was never given.

    A fabricated id is the clearest possible signal of invention: the model had a
    closed list of references in its context and produced something else.
    """
    cited = collect_evidence_ids(payload)
    unknown = sorted(cited - allowed)
    if unknown:
        raise EvidenceMissing(
            f"task {task!r} cited {len(unknown)} evidence reference(s) that were not supplied",
            details={"unknown_evidence_ids": unknown, "allowed_count": len(allowed)},
        )


def find_unsupported_metrics(text: str, supporting_texts: list[str]) -> list[str]:
    """Return quantitative claims in ``text`` absent from all supporting texts.

    Used by resume tailoring: a rewritten bullet may rephrase freely, but any
    number it states must already appear in the candidate's own evidence.
    """
    haystack = " ".join(supporting_texts).lower()
    unsupported: list[str] = []
    for match in METRIC_PATTERN.finditer(text):
        claim = match.group(0).strip()
        digits = re.sub(r"[^\d.]", "", claim)
        if digits and digits not in haystack:
            unsupported.append(claim)
    return unsupported
