"""Simulated resume-extractor behaviours for the stub provider.

Rather than hardcoding a fixture payload (which would break the moment evidence
ids change), these read the evidence listing out of the request the pipeline
actually built and respond as a model would. That means the tests exercise the
real prompt construction, the real evidence ids and the real guards.

Three behaviours are modelled:

* ``well_behaved`` — restates cited text faithfully.
* ``fabricating_*`` — the failure modes the guards exist to catch.
* ``verbose`` — cites several blocks for one claim, to check merging.
"""

from __future__ import annotations

import re
from typing import Any

from packages.core.llm.base import LLMRequest

#: Matches the lines produced by ``prompts.candidate.format_evidence_blocks``.
_LISTING_RE = re.compile(
    r"^\[(?P<id>ev_[0-9a-f]+)\] \((?P<section>[^/]+)/(?P<kind>[^)]+)\) (?P<text>.+)$"
)


class EvidenceBlock:
    __slots__ = ("evidence_id", "kind", "section", "text")

    def __init__(self, evidence_id: str, section: str, kind: str, text: str) -> None:
        self.evidence_id = evidence_id
        self.section = section
        self.kind = kind
        self.text = text


def parse_listing(request: LLMRequest[Any]) -> list[EvidenceBlock]:
    """Recover the evidence blocks the pipeline sent to the model."""
    blocks: list[EvidenceBlock] = []
    for line in request.render_user_message().splitlines():
        match = _LISTING_RE.match(line.strip())
        if match is not None:
            blocks.append(
                EvidenceBlock(
                    match.group("id"),
                    match.group("section"),
                    match.group("kind"),
                    match.group("text"),
                )
            )
    return blocks


def well_behaved(request: LLMRequest[Any]) -> dict[str, Any]:
    """Propose facts that restate cited evidence verbatim.

    Verbatim restatement is what a compliant model does on the conservative end,
    and it means every guard should pass.
    """
    facts: list[dict[str, Any]] = []

    for block in parse_listing(request):
        if block.kind == "bullet":
            category = "achievement" if block.section == "experience" else "project"
            facts.append(
                {
                    "category": category,
                    "claim": block.text,
                    "evidence_ids": [block.evidence_id],
                    "attributes": {},
                    "confidence": 0.95,
                }
            )
        elif block.kind == "entry" and block.section == "experience":
            facts.append(
                {
                    "category": "experience",
                    "claim": block.text,
                    "evidence_ids": [block.evidence_id],
                    "attributes": {},
                    "confidence": 0.9,
                }
            )
        elif block.section == "summary" and block.kind == "line":
            facts.append(
                {
                    "category": "summary",
                    "claim": block.text,
                    "evidence_ids": [block.evidence_id],
                    "attributes": {},
                    "confidence": 0.85,
                }
            )

    return {"facts": facts, "uncertain": []}


def _first_block(request: LLMRequest[Any]) -> EvidenceBlock:
    blocks = parse_listing(request)
    if not blocks:  # pragma: no cover - would mean the prompt was built wrong
        raise AssertionError("no evidence blocks were sent to the extractor")
    return blocks[0]


def fabricating_evidence_id(request: LLMRequest[Any]) -> dict[str, Any]:
    """Cite an id that was never supplied."""
    return {
        "facts": [
            {
                "category": "experience",
                "claim": "Staff Engineer at Globex Corporation",
                "evidence_ids": ["ev_" + "f" * 32],
                "attributes": {},
                "confidence": 0.95,
            }
        ],
        "uncertain": [],
    }


def fabricating_metric(request: LLMRequest[Any]) -> dict[str, Any]:
    """Attach an impressive number the evidence does not contain."""
    block = _first_block(request)
    return {
        "facts": [
            {
                "category": "achievement",
                "claim": "Improved system throughput by 250% and saved $2M annually",
                "evidence_ids": [block.evidence_id],
                "attributes": {},
                "confidence": 0.9,
            }
        ],
        "uncertain": [],
    }


def fabricating_employer(request: LLMRequest[Any]) -> dict[str, Any]:
    """Claim a plausible-sounding employer that appears nowhere in the resume."""
    block = _first_block(request)
    return {
        "facts": [
            {
                "category": "experience",
                "claim": "Senior Engineer",
                "evidence_ids": [block.evidence_id],
                "attributes": {"employer": "Initech Global"},
                "confidence": 0.9,
            }
        ],
        "uncertain": [],
    }


def fabricating_date(request: LLMRequest[Any]) -> dict[str, Any]:
    """Invent a year that is absent from the cited evidence."""
    block = _first_block(request)
    return {
        "facts": [
            {
                "category": "education",
                "claim": "Completed doctorate in 2011",
                "evidence_ids": [block.evidence_id],
                "attributes": {},
                "confidence": 0.8,
            }
        ],
        "uncertain": [],
    }


def unattributed_observation(request: LLMRequest[Any]) -> dict[str, Any]:
    """Propose a fact with no citation at all, plus an uncertain note."""
    return {
        "facts": [
            {
                "category": "skill",
                "claim": "Leadership",
                "evidence_ids": [],
                "attributes": {},
                "confidence": 0.5,
            }
        ],
        "uncertain": ["candidate may have management experience"],
    }


# --------------------------------------------------------------- resume editor


def _targets(request: LLMRequest[Any]) -> list[tuple[str, str, str]]:
    """Recover (target_id, evidence_id, current_text) from an editor prompt."""
    rendered = request.render_user_message()
    found: list[tuple[str, str, str]] = []
    lines = rendered.splitlines()
    for index, line in enumerate(lines):
        match = re.match(
            r"- target_id=(?P<target>\S+) \((?P<kind>[^)]+)\) \[evidence: (?P<ev>[^\]]+)\]",
            line.strip(),
        )
        if match and index + 1 < len(lines):
            found.append((match.group("target"), match.group("ev"), lines[index + 1].strip()))
    return found


def faithful_editor(request: LLMRequest[Any]) -> dict[str, Any]:
    """Rephrase one bullet without changing any fact."""
    for target_id, evidence_id, text in _targets(request):
        if "Mentored" not in text:
            continue
        return {
            "edits": [
                {
                    "target_id": target_id,
                    "old_text": text,
                    "new_text": "Mentored three engineers as they joined the vision team",
                    "evidence_ids": [] if evidence_id == "none" else [evidence_id],
                    "rationale": "Leads with mentoring, which the posting asks for",
                    "confidence": 0.9,
                }
            ],
            "unaddressed_requirements": [],
        }
    return {"edits": [], "unaddressed_requirements": []}


def metric_inventing_editor(request: LLMRequest[Any]) -> dict[str, Any]:
    """Add a figure the evidence does not contain."""
    for target_id, evidence_id, text in _targets(request):
        if "Mentored" not in text:
            continue
        return {
            "edits": [
                {
                    "target_id": target_id,
                    "old_text": text,
                    "new_text": "Mentored 12 engineers, improving team velocity by 40%",
                    "evidence_ids": [] if evidence_id == "none" else [evidence_id],
                    "rationale": "Sounds stronger",
                    "confidence": 0.9,
                }
            ],
            "unaddressed_requirements": [],
        }
    return {"edits": [], "unaddressed_requirements": []}


def latex_injecting_editor(request: LLMRequest[Any]) -> dict[str, Any]:
    """Try to smuggle a LaTeX command into the document."""
    for target_id, evidence_id, text in _targets(request):
        if "Mentored" not in text:
            continue
        return {
            "edits": [
                {
                    "target_id": target_id,
                    "old_text": text,
                    "new_text": "Mentored engineers \\input{/etc/passwd}",
                    "evidence_ids": [] if evidence_id == "none" else [evidence_id],
                    "rationale": "",
                    "confidence": 0.9,
                }
            ],
            "unaddressed_requirements": [],
        }
    return {"edits": [], "unaddressed_requirements": []}


def stale_editor(request: LLMRequest[Any]) -> dict[str, Any]:
    """Propose an edit against text that is not in the document."""
    targets = _targets(request)
    target_id = targets[0][0] if targets else "experience.1.bullet.1"
    return {
        "edits": [
            {
                "target_id": target_id,
                "old_text": "Some text that was never in this resume",
                "new_text": "A replacement",
                "evidence_ids": [],
                "rationale": "",
                "confidence": 0.9,
            }
        ],
        "unaddressed_requirements": ["Kubernetes at scale"],
    }
