"""Candidate profile, canonical facts, and target-role preferences.

Preferences are modelled separately from resume facts on purpose (FR-06): what a
candidate *wants* is not evidence of what they have done.
"""

from __future__ import annotations

from pydantic import Field

from packages.schemas.common import Claim, Confidence, EvidenceRef, Schema
from packages.schemas.enums import EmploymentType, FactCategory, Provenance, RemoteMode


class CandidateFact(Schema):
    """One canonical, evidence-linked fact about the candidate (PRD FR-04)."""

    id: str
    candidate_id: str
    category: FactCategory
    claim: str = Field(min_length=1, description="The normalized assertion")
    # Structured payload for category-specific fields (employer, dates, degree...).
    attributes: dict[str, object] = Field(default_factory=dict)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    confidence: Confidence = 0.0
    provenance: Provenance = Provenance.UNKNOWN
    verified: bool = False

    def as_claim(self) -> Claim:
        return Claim(
            text=self.claim,
            provenance=self.provenance,
            confidence=self.confidence,
            verified=self.verified,
            evidence=self.evidence,
        )


class TargetRole(Schema):
    """A role the candidate is aiming for."""

    title: str
    seniority: str | None = None
    keywords: list[str] = Field(default_factory=list)


class CandidatePreferences(Schema):
    """What the candidate wants — never treated as resume evidence."""

    target_roles: list[TargetRole] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_modes: list[RemoteMode] = Field(default_factory=list)
    employment_types: list[EmploymentType] = Field(default_factory=list)
    min_salary: int | None = Field(default=None, ge=0)
    salary_currency: str | None = Field(default=None, max_length=3)
    # Free-text exclusions, e.g. "no defence contractors".
    exclusions: list[str] = Field(default_factory=list)
    willing_to_relocate: bool | None = None
    notice_period_days: int | None = Field(default=None, ge=0)
    # Deliberately nullable: an unset authorization is UNKNOWN, never assumed.
    work_authorization: dict[str, str] = Field(
        default_factory=dict,
        description="Country code -> status, e.g. {'US': 'requires_sponsorship'}",
    )


class CandidateProfile(Schema):
    """Aggregate view returned by the API for the profile screen."""

    id: str
    display_name: str | None = None
    preferences: CandidatePreferences = Field(default_factory=CandidatePreferences)
    facts: list[CandidateFact] = Field(default_factory=list)

    def facts_by_category(self, category: FactCategory) -> list[CandidateFact]:
        return [fact for fact in self.facts if fact.category is category]

    @property
    def unresolved_count(self) -> int:
        """Facts still needing human confirmation — surfaced on the dashboard."""
        return sum(
            1 for fact in self.facts if fact.provenance is Provenance.UNKNOWN or not fact.verified
        )
