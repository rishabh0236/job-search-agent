"""ORM models implementing the PRD §9 data model.

Design notes:
* Identifiers are prefixed strings (``packages.core.ids``), not integers, so
  records stay traceable in logs and portable across stores.
* Enums are stored as their string values for readable SQLite dumps.
* The original resume is protected by an application-level invariant plus a
  partial index on ``(candidate_id, is_original)``.
* ``applications`` carries a uniqueness constraint on ``(candidate_id, job_id)``
  and an idempotency key — the duplicate-submit prevention the PRD asks for.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from packages.core.db.base import Base
from packages.core.db.types import StrEnumType, utcnow
from packages.schemas.enums import (
    ApplicationStatus,
    ArtifactType,
    EditOperation,
    EditStatus,
    Eligibility,
    FactCategory,
    Provenance,
    SourceType,
)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(default=utcnow, onupdate=utcnow)


class Candidate(Base, TimestampMixin):
    __tablename__ = "candidates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str | None] = mapped_column(String(200), default=None)
    #: Serialized ``CandidatePreferences``. Kept apart from facts by design.
    preferences: Mapped[dict[str, Any]] = mapped_column(default=dict)

    facts: Mapped[list[CandidateFact]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )
    resumes: Mapped[list[Resume]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )


class Evidence(Base):
    """Verbatim source text supporting one or more claims.

    Stored once and referenced, so the same sentence backing three skills is not
    duplicated — and so deleting a source cascades to everything it supported.
    """

    __tablename__ = "evidence"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    source_id: Mapped[str] = mapped_column(String(64), index=True)
    locator: Mapped[str] = mapped_column(String(200))
    quote: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class FactEvidence(Base):
    """Association between a fact and the evidence that supports it."""

    __tablename__ = "fact_evidence"

    fact_id: Mapped[str] = mapped_column(
        ForeignKey("candidate_facts.id", ondelete="CASCADE"), primary_key=True
    )
    evidence_id: Mapped[str] = mapped_column(
        ForeignKey("evidence.id", ondelete="CASCADE"), primary_key=True
    )


class CandidateFact(Base, TimestampMixin):
    __tablename__ = "candidate_facts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    category: Mapped[FactCategory] = mapped_column(StrEnumType(FactCategory, 40), index=True)
    claim: Mapped[str] = mapped_column(Text)
    attributes: Mapped[dict[str, Any]] = mapped_column(default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    provenance: Mapped[Provenance] = mapped_column(
        StrEnumType(Provenance, 20), default=Provenance.UNKNOWN
    )
    verified: Mapped[bool] = mapped_column(Boolean, default=False)

    candidate: Mapped[Candidate] = relationship(back_populates="facts")
    evidence: Mapped[list[Evidence]] = relationship(secondary="fact_evidence")

    __table_args__ = (
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_fact_confidence_range"),
    )


class Resume(Base):
    """A resume version. The original (``is_original``) is never mutated."""

    __tablename__ = "resumes"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    source_type: Mapped[SourceType] = mapped_column(StrEnumType(SourceType, 20))
    source_path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    derived_from_id: Mapped[str | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), default=None
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), default=None
    )
    is_original: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    candidate: Mapped[Candidate] = relationship(back_populates="resumes")
    edits: Mapped[list[ResumeEdit]] = relationship(
        back_populates="resume", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("candidate_id", "version", name="uq_resume_candidate_version"),
    )


# At most one immutable original per candidate, enforced as a partial unique
# index. Declared at module level because the predicate needs the mapped column,
# which is not yet bound inside the class body. Both dialect spellings are given
# so the constraint survives the SQLite -> PostgreSQL move.
Index(
    "uq_resume_single_original",
    Resume.candidate_id,
    unique=True,
    sqlite_where=Resume.is_original.is_(True),
    postgresql_where=Resume.is_original.is_(True),
)


class ResumeEdit(Base):
    __tablename__ = "resume_edits"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    resume_id: Mapped[str] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"), index=True)
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), default=None
    )
    operation: Mapped[EditOperation] = mapped_column(StrEnumType(EditOperation, 30))
    target_id: Mapped[str] = mapped_column(String(200))
    old_text: Mapped[str] = mapped_column(Text)
    new_text: Mapped[str] = mapped_column(Text, default="")
    #: Evidence ids as a JSON array — an edit references evidence, it does not own it.
    evidence_refs: Mapped[list[str]] = mapped_column(default=list)
    rationale: Mapped[str] = mapped_column(Text, default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[EditStatus] = mapped_column(
        StrEnumType(EditStatus, 20), default=EditStatus.PROPOSED
    )
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    resume: Mapped[Resume] = relationship(back_populates="edits")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    source: Mapped[str] = mapped_column(String(60), index=True)
    source_job_id: Mapped[str] = mapped_column(String(200))
    company: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(300), index=True)
    location: Mapped[str | None] = mapped_column(String(200), default=None)
    remote: Mapped[str] = mapped_column(String(20), default="unknown")
    employment_type: Mapped[str] = mapped_column(String(20), default="unknown")
    description: Mapped[str] = mapped_column(Text, default="")
    requirements_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    salary_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    url: Mapped[str | None] = mapped_column(String(1000), default=None)
    posted_at: Mapped[datetime | None] = mapped_column(default=None)
    retrieved_at: Mapped[datetime] = mapped_column(default=utcnow)
    dedupe_group: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    raw_json: Mapped[dict[str, Any]] = mapped_column(default=dict)

    __table_args__ = (
        # Source ids are unique per source; this is the first dedupe line of defence.
        UniqueConstraint("source", "source_job_id", name="uq_job_source_identity"),
    )


class JobMatch(Base):
    __tablename__ = "job_matches"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    eligibility: Mapped[Eligibility] = mapped_column(
        StrEnumType(Eligibility, 20), default=Eligibility.UNKNOWN
    )
    hard_constraints_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    #: Every weighted component, so a score is always reconstructable (PRD §10).
    components_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    strengths_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    gaps_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    explanation: Mapped[str] = mapped_column(Text, default="")
    weights_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    __table_args__ = (
        UniqueConstraint("candidate_id", "job_id", name="uq_match_candidate_job"),
        CheckConstraint("score >= 0 AND score <= 1", name="ck_match_score_range"),
    )


class Application(Base, TimestampMixin):
    __tablename__ = "applications"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), index=True
    )
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    status: Mapped[ApplicationStatus] = mapped_column(
        StrEnumType(ApplicationStatus, 30), default=ApplicationStatus.CREATED, index=True
    )
    approved_resume_id: Mapped[str | None] = mapped_column(
        ForeignKey("resumes.id", ondelete="SET NULL"), default=None
    )
    cover_letter_artifact_id: Mapped[str | None] = mapped_column(
        ForeignKey("artifacts.id", ondelete="SET NULL"), default=None
    )
    submitted_at: Mapped[datetime | None] = mapped_column(default=None)
    confirmation_ref: Mapped[str | None] = mapped_column(String(300), default=None)
    #: Set once immediately before a submit attempt; unique, so a retry after an
    #: uncertain network response cannot create a second submission.
    idempotency_key: Mapped[str | None] = mapped_column(String(64), default=None)
    stop_reason: Mapped[str | None] = mapped_column(String(40), default=None)
    stop_detail: Mapped[str] = mapped_column(Text, default="")

    answers: Mapped[list[ApplicationAnswer]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )

    __table_args__ = (
        # One application per candidate per job.
        UniqueConstraint("candidate_id", "job_id", name="uq_application_candidate_job"),
        UniqueConstraint("idempotency_key", name="uq_application_idempotency"),
    )


class ApplicationAnswer(Base):
    __tablename__ = "application_answers"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("applications.id", ondelete="CASCADE"), index=True
    )
    field: Mapped[str] = mapped_column(String(300))
    question: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    source: Mapped[Provenance] = mapped_column(
        StrEnumType(Provenance, 20), default=Provenance.UNKNOWN
    )
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    user_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    application: Mapped[Application] = relationship(back_populates="answers")

    __table_args__ = (
        UniqueConstraint("application_id", "field", name="uq_answer_application_field"),
    )


class Artifact(Base):
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[ArtifactType] = mapped_column(StrEnumType(ArtifactType, 40), index=True)
    path: Mapped[str] = mapped_column(String(500))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    application_id: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class AuditEvent(Base):
    """Append-only audit log. Never updated, never deleted by application code."""

    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor: Mapped[str] = mapped_column(String(80), index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    entity_type: Mapped[str] = mapped_column(String(60), index=True)
    entity_id: Mapped[str] = mapped_column(String(64), index=True)
    timestamp: Mapped[datetime] = mapped_column(default=utcnow, index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(default=dict)
