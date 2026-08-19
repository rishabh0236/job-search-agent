"""Normalized job postings and the adapter contract.

Every job carries provenance (source, source id, canonical url, retrieval time)
so a match can always be traced back to what was actually fetched (FR-14).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, HttpUrl

from packages.schemas.common import Schema
from packages.schemas.enums import EmploymentType, RemoteMode, RequirementKind


class SalaryRange(Schema):
    min_amount: int | None = Field(default=None, ge=0)
    max_amount: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, max_length=3)
    period: str | None = Field(default=None, description="year | month | hour")


class JobRequirement(Schema):
    """One extracted requirement, classified as a gate or a preference."""

    text: str = Field(min_length=1)
    kind: RequirementKind = RequirementKind.CONTEXTUAL
    # Normalized handle for scoring, e.g. "python", "years_experience>=5".
    key: str | None = None


class Job(Schema):
    """A normalized posting (PRD FR-12)."""

    id: str
    source: str = Field(description="Adapter name, e.g. 'greenhouse'")
    source_job_id: str
    company: str
    title: str
    location: str | None = None
    remote: RemoteMode = RemoteMode.UNKNOWN
    employment_type: EmploymentType = EmploymentType.UNKNOWN
    description: str = ""
    requirements: list[JobRequirement] = Field(default_factory=list)
    salary: SalaryRange | None = None
    url: HttpUrl | None = None
    posted_at: datetime | None = None
    retrieved_at: datetime
    # Populated by the dedupe pass; jobs sharing a group are the same posting.
    dedupe_group: str | None = None
    # Raw adapter payload, retained for debugging and re-normalization.
    raw: dict[str, object] = Field(default_factory=dict)

    def required_requirements(self) -> list[JobRequirement]:
        return [r for r in self.requirements if r.kind is RequirementKind.REQUIRED]


class JobSearchCriteria(Schema):
    """Input to ``JobSource.search`` (FR-10)."""

    titles: list[str] = Field(default_factory=list)
    locations: list[str] = Field(default_factory=list)
    remote_modes: list[RemoteMode] = Field(default_factory=list)
    employment_types: list[EmploymentType] = Field(default_factory=list)
    min_salary: int | None = Field(default=None, ge=0)
    keywords: list[str] = Field(default_factory=list)
    posted_within_days: int | None = Field(default=None, ge=0)
    limit: int = Field(default=50, ge=1, le=500)


class SourceHealth(Schema):
    """Result of ``JobSource.health_check`` — shown on the discovery screen."""

    source: str
    healthy: bool
    detail: str | None = None
    checked_at: datetime
