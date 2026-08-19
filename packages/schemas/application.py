"""Applications, answers, artifacts and audit events."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from packages.schemas.common import Confidence, Schema
from packages.schemas.enums import (
    APPLICATION_TRANSITIONS,
    ApplicationStatus,
    ArtifactType,
    Provenance,
    StopReason,
)


class FormField(Schema):
    """One field discovered on an application form.

    Produced by the browser layer (or a fixture) and consumed by answer mapping.
    ``sensitive`` is decided by deterministic rules, never by a model: a field that
    asks about salary, authorization or demographics always needs the human.
    """

    name: str = Field(min_length=1, description="Form field name or stable handle")
    label: str = ""
    field_type: str = Field(
        default="text", description="text|email|tel|select|checkbox|file|textarea|number"
    )
    required: bool = False
    options: list[str] = Field(default_factory=list, description="For select/radio fields")
    max_length: int | None = None


class FormSpec(Schema):
    """The set of fields on one application form."""

    url: str = ""
    fields: list[FormField] = Field(default_factory=list)
    submit_selector: str = ""

    def required_names(self) -> list[str]:
        return [field.name for field in self.fields if field.required]


class ApplicationAnswer(Schema):
    """A single form answer with its provenance (FR-42/FR-43).

    ``user_verified`` is required before submission for anything the system did
    not read straight off a verified candidate fact.
    """

    id: str
    application_id: str
    field: str = Field(description="Form field label or stable selector handle")
    question: str = ""
    answer: str = ""
    source: Provenance = Provenance.UNKNOWN
    confidence: Confidence = 0.0
    user_verified: bool = False
    # True for authorization, salary, notice period, demographics — never guessed.
    sensitive: bool = False

    @property
    def needs_user(self) -> bool:
        if self.sensitive or self.source in (Provenance.UNKNOWN, Provenance.AI):
            return not self.user_verified
        return False


class Artifact(Schema):
    """Any file produced or consumed by a run, content-addressed for audit."""

    id: str
    type: ArtifactType
    path: str
    sha256: str = Field(min_length=64, max_length=64)
    application_id: str | None = None
    created_at: datetime


class Application(Schema):
    """One application to one job (PRD §9).

    Uniqueness on ``(candidate_id, job_id)`` is enforced in the database, and
    ``idempotency_key`` guards against a duplicate submit when the network state
    after a click is uncertain — the PRD requires duplicate-submit prevention but
    gave it no schema support.
    """

    id: str
    candidate_id: str
    job_id: str
    status: ApplicationStatus = ApplicationStatus.CREATED
    approved_resume_id: str | None = None
    cover_letter_artifact_id: str | None = None
    answers: list[ApplicationAnswer] = Field(default_factory=list)
    submitted_at: datetime | None = None
    confirmation_ref: str | None = None
    idempotency_key: str | None = None
    stop_reason: StopReason | None = None
    stop_detail: str = ""

    def can_transition_to(self, target: ApplicationStatus) -> bool:
        return target in APPLICATION_TRANSITIONS[self.status]

    @property
    def unresolved_answers(self) -> list[ApplicationAnswer]:
        return [answer for answer in self.answers if answer.needs_user]

    def submission_blockers(self) -> list[str]:
        """Pre-submit checklist (FR-53). Empty list means safe to offer Submit."""
        blockers: list[str] = []
        if self.approved_resume_id is None:
            blockers.append("no approved resume attached")
        if self.status is not ApplicationStatus.USER_APPROVED:
            blockers.append(f"status is {self.status.value}, expected user_approved")
        for answer in self.unresolved_answers:
            blockers.append(f"answer requires confirmation: {answer.field}")
        if self.submitted_at is not None:
            blockers.append("already submitted")
        return blockers


class AuditEvent(Schema):
    """An append-only record of a consequential action (PRD §9)."""

    id: str
    actor: str = Field(description="'user', 'system', or an agent task name")
    action: str
    entity_type: str
    entity_id: str
    timestamp: datetime
    metadata: dict[str, object] = Field(default_factory=dict)
