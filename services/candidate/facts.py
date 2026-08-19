"""Validation and persistence of candidate facts.

Every fact — whether a regex found it or a model proposed it — passes through
``validate`` before it can be stored. The checks are deliberately mechanical:

* **Fabricated citation** -> reject outright. The model had a closed list of
  evidence ids; producing another one is invention, not imprecision.
* **Unsupported number** -> reject. A metric that does not appear in the cited
  evidence is the single most damaging hallucination in a resume.
* **Unsupported year** -> reject. Same reasoning, applied to dates.
* **Unsupported employer/institution** -> reject. Blocks an invented workplace even
  when the surrounding sentence is plausible.
* **No evidence at all** -> store as UNKNOWN, never as fact. The user can confirm
  it; the system will not assert it.

Fact ids are content-addressed, so re-ingesting the same resume updates facts in
place instead of duplicating the profile.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.db.models import CandidateFact, Evidence, FactEvidence
from packages.core.llm.guards import find_unsupported_metrics
from packages.schemas.enums import FactCategory, Provenance
from packages.schemas.ingestion import IngestionFinding
from packages.schemas.llm_tasks import ProposedFact
from services.candidate.evidence import normalize_for_comparison
from services.candidate.parsing import DeterministicFact

YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")

#: Attribute keys naming a real-world entity. If the source does not contain the
#: value, the fact is fabricated regardless of how reasonable it sounds.
ENTITY_ATTRIBUTES = ("employer", "company", "institution", "school", "university", "issuer")

#: Categories where an invented claim causes direct harm on an application.
HIGH_RISK_CATEGORIES = frozenset(
    {
        FactCategory.EXPERIENCE,
        FactCategory.ACHIEVEMENT,
        FactCategory.EDUCATION,
        FactCategory.CERTIFICATION,
        FactCategory.PUBLICATION,
        FactCategory.WORK_AUTHORIZATION,
        FactCategory.COMPENSATION,
    }
)


@dataclass(slots=True)
class ValidatedFact:
    category: FactCategory
    claim: str
    evidence_ids: list[str]
    attributes: dict[str, str] = field(default_factory=dict)
    confidence: float = 0.0
    provenance: Provenance = Provenance.RESUME
    verified: bool = False


def fact_id_for(candidate_id: str, category: FactCategory, claim: str) -> str:
    """Content-addressed fact id, so re-ingestion is idempotent."""
    key = f"{candidate_id}|{category.value}|{normalize_for_comparison(claim)}"
    return f"fact_{hashlib.sha256(key.encode()).hexdigest()[:32]}"


def _unsupported_years(claim: str, evidence_texts: list[str]) -> list[str]:
    haystack = " ".join(evidence_texts)
    return [year for year in set(YEAR_RE.findall(claim)) if year not in haystack]


def _unsupported_entities(attributes: dict[str, str], evidence_texts: list[str]) -> list[str]:
    haystack = normalize_for_comparison(" ".join(evidence_texts))
    unsupported: list[str] = []
    for key, value in attributes.items():
        if key.lower() not in ENTITY_ATTRIBUTES:
            continue
        needle = normalize_for_comparison(value)
        if needle and needle not in haystack:
            unsupported.append(f"{key}={value}")
    return unsupported


def validate(
    *,
    category: FactCategory,
    claim: str,
    evidence_ids: list[str],
    attributes: dict[str, str],
    confidence: float,
    available_evidence: dict[str, Evidence],
    provenance: Provenance = Provenance.RESUME,
) -> tuple[ValidatedFact | None, list[IngestionFinding]]:
    """Validate one candidate fact.

    Returns ``(fact, findings)``. A None fact means the proposal was rejected and
    must not be stored.
    """
    findings: list[IngestionFinding] = []

    unknown_ids = [key for key in evidence_ids if key not in available_evidence]
    if unknown_ids:
        findings.append(
            IngestionFinding(
                severity="error",
                code="fabricated_evidence",
                message=(
                    f"claim cites {len(unknown_ids)} evidence reference(s) that do not exist; "
                    "rejected as unsupported"
                ),
                claim=claim,
            )
        )
        return None, findings

    cited = [available_evidence[key] for key in evidence_ids]
    evidence_texts = [record.quote for record in cited]

    if not cited:
        # Not an error: an observation without evidence is legitimate input, it just
        # cannot be asserted as fact. It becomes a review item.
        findings.append(
            IngestionFinding(
                severity="warning",
                code="no_evidence",
                message="no supporting text found; stored as unknown pending confirmation",
                claim=claim,
            )
        )
        return (
            ValidatedFact(
                category=category,
                claim=claim,
                evidence_ids=[],
                attributes=attributes,
                confidence=0.0,
                provenance=Provenance.UNKNOWN,
                verified=False,
            ),
            findings,
        )

    unsupported_metrics = find_unsupported_metrics(claim, evidence_texts)
    if unsupported_metrics:
        findings.append(
            IngestionFinding(
                severity="error",
                code="unsupported_metric",
                message=(
                    "claim states figures absent from the cited evidence: "
                    f"{', '.join(unsupported_metrics)}"
                ),
                locator=cited[0].locator,
                claim=claim,
            )
        )
        return None, findings

    unsupported_years = _unsupported_years(claim, evidence_texts)
    if unsupported_years:
        findings.append(
            IngestionFinding(
                severity="error",
                code="unsupported_date",
                message=f"claim states dates absent from the cited evidence: {', '.join(sorted(unsupported_years))}",
                locator=cited[0].locator,
                claim=claim,
            )
        )
        return None, findings

    unsupported_entities = _unsupported_entities(attributes, evidence_texts)
    if unsupported_entities:
        findings.append(
            IngestionFinding(
                severity="error",
                code="unsupported_entity",
                message=(
                    "claim names entities absent from the cited evidence: "
                    f"{', '.join(unsupported_entities)}"
                ),
                locator=cited[0].locator,
                claim=claim,
            )
        )
        return None, findings

    # High-risk categories start unverified even at high model confidence: the user
    # confirms them on the review screen before they can reach an application.
    resolved_confidence = min(1.0, max(0.0, confidence))
    if category in HIGH_RISK_CATEGORIES:
        resolved_confidence = min(resolved_confidence, 0.9)

    return (
        ValidatedFact(
            category=category,
            claim=claim,
            evidence_ids=evidence_ids,
            attributes=attributes,
            confidence=resolved_confidence,
            provenance=provenance,
            verified=False,
        ),
        findings,
    )


def validate_proposal(
    proposal: ProposedFact,
    available_evidence: dict[str, Evidence],
) -> tuple[ValidatedFact | None, list[IngestionFinding]]:
    """Validate an LLM-proposed fact."""
    return validate(
        category=proposal.category,
        claim=proposal.claim,
        evidence_ids=list(dict.fromkeys(proposal.evidence_ids)),
        attributes=dict(proposal.attributes),
        confidence=proposal.confidence,
        available_evidence=available_evidence,
        provenance=Provenance.RESUME,
    )


def validate_deterministic(
    fact: DeterministicFact,
    locator_to_evidence: dict[str, Evidence],
) -> tuple[ValidatedFact | None, list[IngestionFinding]]:
    """Validate a code-extracted fact.

    Runs the same checks as an LLM proposal. A regex is more trustworthy than a
    model, but "more trustworthy" is not a reason to skip verification.
    """
    evidence_ids = [
        locator_to_evidence[locator].id
        for locator in fact.locators
        if locator in locator_to_evidence
    ]
    return validate(
        category=fact.category,
        claim=fact.claim,
        evidence_ids=evidence_ids,
        attributes=dict(fact.attributes),
        confidence=fact.confidence,
        available_evidence={record.id: record for record in locator_to_evidence.values()},
        provenance=Provenance.RESUME,
    )


def persist(
    session: Session,
    *,
    candidate_id: str,
    facts: list[ValidatedFact],
) -> tuple[int, int]:
    """Upsert validated facts. Returns ``(created, updated)``.

    Merging by content-addressed id means a second ingestion of the same resume
    adds evidence to existing facts rather than duplicating the profile, and a fact
    the user already verified stays verified.
    """
    created = updated = 0

    for fact in facts:
        fact_id = fact_id_for(candidate_id, fact.category, fact.claim)
        existing = session.get(CandidateFact, fact_id)

        if existing is None:
            session.add(
                CandidateFact(
                    id=fact_id,
                    candidate_id=candidate_id,
                    category=fact.category,
                    claim=fact.claim,
                    attributes=dict(fact.attributes),
                    confidence=fact.confidence,
                    provenance=fact.provenance,
                    verified=fact.verified,
                )
            )
            created += 1
        else:
            # Never downgrade a human decision or a stronger provenance.
            if (
                existing.provenance is Provenance.UNKNOWN
                and fact.provenance is not Provenance.UNKNOWN
            ):
                existing.provenance = fact.provenance
            existing.confidence = max(existing.confidence, fact.confidence)
            existing.attributes = {**existing.attributes, **fact.attributes}
            updated += 1

        session.flush()

        linked = set(
            session.scalars(select(FactEvidence.evidence_id).where(FactEvidence.fact_id == fact_id))
        )
        for evidence_id in fact.evidence_ids:
            if evidence_id not in linked:
                session.add(FactEvidence(fact_id=fact_id, evidence_id=evidence_id))
                linked.add(evidence_id)

    session.flush()
    return created, updated
