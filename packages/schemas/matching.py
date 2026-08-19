"""Match scoring output.

The weights come from PRD §10 and are configurable (FR-24). Every component is
logged so a score is always explainable rather than a bare number.
"""

from __future__ import annotations

from pydantic import Field, model_validator

from packages.schemas.common import EvidenceRef, Schema
from packages.schemas.enums import Eligibility


class ScoreWeights(Schema):
    """Configurable component weights. Defaults are the PRD's initial values."""

    hard_constraints: float = Field(default=0.30, ge=0, le=1)
    required_skill_evidence: float = Field(default=0.25, ge=0, le=1)
    semantic_experience_fit: float = Field(default=0.20, ge=0, le=1)
    seniority: float = Field(default=0.10, ge=0, le=1)
    location_preferences: float = Field(default=0.05, ge=0, le=1)
    preferred_skills: float = Field(default=0.05, ge=0, le=1)
    other_preferences: float = Field(default=0.05, ge=0, le=1)

    @model_validator(mode="after")
    def _weights_sum_to_one(self) -> ScoreWeights:
        total = sum(self.model_dump().values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"score weights must sum to 1.0, got {total:.4f}")
        return self

    def as_dict(self) -> dict[str, float]:
        return {key: float(value) for key, value in self.model_dump().items()}


class ScoreComponent(Schema):
    """One weighted contribution to the final score."""

    name: str
    raw_score: float = Field(ge=0, le=1, description="Component score before weighting")
    weight: float = Field(ge=0, le=1)
    rationale: str = ""

    @property
    def weighted(self) -> float:
        return self.raw_score * self.weight


class MatchedRequirement(Schema):
    """A requirement paired with the evidence that satisfies it — or does not."""

    requirement: str
    satisfied: bool | None = Field(
        default=None,
        description="None means unknown; a missing fact is not a negative",
    )
    evidence: list[EvidenceRef] = Field(default_factory=list)
    note: str = ""


class HardConstraintResult(Schema):
    """Outcome of the eligibility gate (FR-21)."""

    eligibility: Eligibility = Eligibility.UNKNOWN
    blocking: list[str] = Field(
        default_factory=list, description="Constraints the candidate fails outright"
    )
    unknown: list[str] = Field(
        default_factory=list, description="Constraints needing user confirmation"
    )


class JobMatch(Schema):
    """Explainable match result (PRD FR-22/FR-23)."""

    id: str
    job_id: str
    candidate_id: str
    score: float = Field(ge=0, le=1)
    eligibility: Eligibility = Eligibility.UNKNOWN
    hard_constraints: HardConstraintResult = Field(default_factory=HardConstraintResult)
    components: list[ScoreComponent] = Field(default_factory=list)
    strengths: list[MatchedRequirement] = Field(default_factory=list)
    gaps: list[MatchedRequirement] = Field(default_factory=list)
    # LLM-authored prose, required to cite evidence ids it was given.
    explanation: str = ""
    uncertainty: list[str] = Field(
        default_factory=list, description="What the system could not determine"
    )
    weights_used: dict[str, float] = Field(default_factory=dict)

    def recompute_score(self) -> float:
        """Deterministic re-derivation from components; guards LLM drift."""
        return round(min(1.0, sum(component.weighted for component in self.components)), 6)
