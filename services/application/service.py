"""Application preparation and the submission gate.

The state machine from skills/06 is enforced here, in deterministic code:

    CREATED -> PREPARING -> READY_FOR_REVIEW -> USER_APPROVED
            -> SUBMITTING -> SUBMITTED
    alternates: VERIFICATION_REQUIRED / FAILED / STOPPED

Three properties are non-negotiable and each is enforced by a separate mechanism, so
no single mistake can defeat them:

* **No submission without explicit human approval.** The transition table makes
  ``SUBMITTING`` reachable only from ``USER_APPROVED``, and ``approve()`` is the only
  way into that state.
* **No submission with an unresolved answer.** The pre-submit checklist blocks on any
  sensitive or model-authored answer the user has not confirmed.
* **No duplicate submission.** A unique ``(candidate_id, job_id)`` constraint plus an
  idempotency key claimed *before* the attempt, so a retry after an uncertain network
  response cannot create a second application.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core import audit
from packages.core.db.models import (
    Application as ApplicationRow,
)
from packages.core.db.models import (
    ApplicationAnswer as AnswerRow,
)
from packages.core.db.models import (
    Artifact as ArtifactRow,
)
from packages.core.db.models import (
    Candidate,
    CandidateFact,
    Evidence,
)
from packages.core.db.models import (
    Job as JobRow,
)
from packages.core.db.models import (
    Resume as ResumeRow,
)
from packages.core.errors import (
    ConflictError,
    DomainError,
    NotFoundError,
    SafetyStop,
    ValidationFailed,
)
from packages.core.ids import new_id
from packages.core.llm.base import LLMRequest, UntrustedContent
from packages.core.llm.client import LLMClient
from packages.core.logging import get_logger
from packages.core.settings import Settings, get_settings
from packages.prompts.application import (
    ANSWER_MAPPER_SYSTEM,
    ANSWER_MAPPER_VERSION,
    COVER_LETTER_SYSTEM,
    COVER_LETTER_VERSION,
    answer_mapper_user_message,
    cover_letter_user_message,
)
from packages.schemas.application import (
    Application,
    ApplicationAnswer,
    FormField,
    FormSpec,
)
from packages.schemas.enums import (
    APPLICATION_TRANSITIONS,
    ApplicationStatus,
    ArtifactType,
    Provenance,
    StopReason,
)
from packages.schemas.llm_tasks import (
    TASK_ANSWER_MAPPER,
    TASK_COVER_LETTER,
    AnswerMappingOutput,
    CoverLetterOutput,
)
from services.application import answers as answer_mapping
from services.candidate.extraction import sha256_bytes
from services.jobs.service import row_to_job

logger = get_logger(__name__)


class ApplicationService:
    def __init__(
        self,
        session: Session,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._llm = llm
        self._settings = settings or get_settings()

    # ------------------------------------------------------------------ helpers

    def _row(self, application_id: str) -> ApplicationRow:
        row = self._session.get(ApplicationRow, application_id)
        if row is None:
            raise NotFoundError(f"application {application_id} not found")
        return row

    def _to_schema(self, row: ApplicationRow) -> Application:
        return Application(
            id=row.id,
            candidate_id=row.candidate_id,
            job_id=row.job_id,
            status=row.status,
            approved_resume_id=row.approved_resume_id,
            cover_letter_artifact_id=row.cover_letter_artifact_id,
            answers=[
                ApplicationAnswer(
                    id=answer.id,
                    application_id=answer.application_id,
                    field=answer.field,
                    question=answer.question,
                    answer=answer.answer,
                    source=answer.source,
                    confidence=answer.confidence,
                    user_verified=answer.user_verified,
                    sensitive=answer.sensitive,
                )
                for answer in sorted(row.answers, key=lambda item: item.field)
            ],
            submitted_at=row.submitted_at,
            confirmation_ref=row.confirmation_ref,
            idempotency_key=row.idempotency_key,
            stop_reason=StopReason(row.stop_reason) if row.stop_reason else None,
            stop_detail=row.stop_detail,
        )

    def _transition(self, row: ApplicationRow, target: ApplicationStatus, *, actor: str) -> None:
        """Move the application, or refuse.

        Every state change goes through here so the transition table is the single
        authority and every change is audited.
        """
        if target not in APPLICATION_TRANSITIONS[row.status]:
            raise ConflictError(
                f"cannot move application from {row.status.value} to {target.value}",
                details={
                    "current": row.status.value,
                    "requested": target.value,
                    "allowed": sorted(item.value for item in APPLICATION_TRANSITIONS[row.status]),
                },
            )
        previous = row.status
        row.status = target
        audit.record(
            self._session,
            actor=actor,
            action="application.status_changed",
            entity_type="application",
            entity_id=row.id,
            metadata={"from": previous.value, "to": target.value},
        )
        self._session.flush()

    def _facts(self, candidate_id: str) -> list[CandidateFact]:
        return list(
            self._session.scalars(
                select(CandidateFact).where(CandidateFact.candidate_id == candidate_id)
            ).all()
        )

    def _evidence(self, candidate_id: str) -> dict[str, Evidence]:
        return {
            row.id: row
            for row in self._session.scalars(
                select(Evidence).where(Evidence.candidate_id == candidate_id)
            )
        }

    # ------------------------------------------------------------------- create

    def create(self, candidate_id: str, job_id: str) -> Application:
        if self._session.get(Candidate, candidate_id) is None:
            raise NotFoundError(f"candidate {candidate_id} not found")
        if self._session.get(JobRow, job_id) is None:
            raise NotFoundError(f"job {job_id} not found")

        existing = self._session.scalar(
            select(ApplicationRow).where(
                ApplicationRow.candidate_id == candidate_id, ApplicationRow.job_id == job_id
            )
        )
        if existing is not None:
            raise ConflictError(
                "an application for this job already exists",
                details={"application_id": existing.id, "status": existing.status.value},
            )

        row = ApplicationRow(id=new_id("application"), candidate_id=candidate_id, job_id=job_id)
        self._session.add(row)
        audit.record(
            self._session,
            actor=audit.ACTOR_USER,
            action="application.created",
            entity_type="application",
            entity_id=row.id,
            metadata={"job_id": job_id},
        )
        self._session.flush()
        return self._to_schema(row)

    def get(self, application_id: str) -> Application:
        return self._to_schema(self._row(application_id))

    def list_for_candidate(self, candidate_id: str) -> list[Application]:
        rows = self._session.scalars(
            select(ApplicationRow)
            .where(ApplicationRow.candidate_id == candidate_id)
            .order_by(ApplicationRow.updated_at.desc())
        ).all()
        return [self._to_schema(row) for row in rows]

    # ------------------------------------------------------------------ prepare

    def prepare(
        self,
        application_id: str,
        form: FormSpec,
        *,
        approved_resume_id: str | None = None,
        write_cover_letter: bool = True,
    ) -> Application:
        """Map answers and draft a cover letter, then hand over for review."""
        row = self._row(application_id)
        # Re-preparing after review is a normal action: the user swaps in a different
        # resume version, or the form changed. The transition table already allows
        # READY_FOR_REVIEW -> PREPARING, so honour it here rather than refusing.
        if row.status in (ApplicationStatus.CREATED, ApplicationStatus.READY_FOR_REVIEW):
            self._transition(row, ApplicationStatus.PREPARING, actor=audit.ACTOR_SYSTEM)
        elif row.status is not ApplicationStatus.PREPARING:
            raise ConflictError(
                f"application is {row.status.value} and can no longer be prepared",
                details={"status": row.status.value},
            )

        if approved_resume_id is not None:
            resume = self._session.get(ResumeRow, approved_resume_id)
            if resume is None or resume.candidate_id != row.candidate_id:
                raise NotFoundError(f"resume {approved_resume_id} not found for this candidate")
            row.approved_resume_id = approved_resume_id

        candidate = self._session.get(Candidate, row.candidate_id)
        facts = self._facts(row.candidate_id)
        evidence = self._evidence(row.candidate_id)

        # Clear previous answers so re-preparing is idempotent rather than additive.
        # clear() relies on delete-orphan cascade and also empties the in-memory
        # collection; deleting rows individually left them in it, and the appended
        # replacements were then added on top of the stale entries.
        row.answers.clear()
        self._session.flush()

        deterministic, remaining = answer_mapping.map_deterministic(
            form.fields,
            facts,
            application_id=row.id,
            display_name=candidate.display_name if candidate else None,
        )
        proposed = self._map_remaining(row, remaining, facts, evidence)

        # Append to the relationship rather than session.add(): the collection was
        # already loaded above to clear it, so inserting rows directly would leave
        # row.answers stale and prepare() would report an application with no
        # answers at all.
        for answer in deterministic + proposed:
            row.answers.append(
                AnswerRow(
                    id=answer.id,
                    application_id=row.id,
                    field=answer.field,
                    question=answer.question,
                    answer=answer.answer,
                    source=answer.source,
                    confidence=answer.confidence,
                    user_verified=answer.user_verified,
                    sensitive=answer.sensitive,
                )
            )

        if write_cover_letter:
            self._draft_cover_letter(row, facts, evidence)

        self._session.flush()
        self._transition(row, ApplicationStatus.READY_FOR_REVIEW, actor=audit.ACTOR_SYSTEM)

        application = self._to_schema(row)
        audit.record(
            self._session,
            actor=audit.ACTOR_SYSTEM,
            action="application.prepared",
            entity_type="application",
            entity_id=row.id,
            metadata={
                "fields": len(form.fields),
                "answered": len([a for a in application.answers if a.answer]),
                "needs_user": len(application.unresolved_answers),
                "answer_prompt_version": ANSWER_MAPPER_VERSION,
            },
        )
        logger.info(
            "application.prepared",
            extra={
                "application_id": row.id,
                "fields": len(form.fields),
                "needs_user": len(application.unresolved_answers),
            },
        )
        return application

    def _map_remaining(
        self,
        row: ApplicationRow,
        fields: list[FormField],
        facts: list[CandidateFact],
        evidence: dict[str, Evidence],
    ) -> list[ApplicationAnswer]:
        """Ask the model about the fields code could not fill."""
        typed = fields
        if not typed:
            return []

        if self._llm is None:
            # No model: every remaining field becomes a user question. Honest, and
            # the review screen is still usable.
            return [
                ApplicationAnswer(
                    id=new_id("application_answer"),
                    application_id=row.id,
                    field=field.name,
                    question=field.label or field.name,
                    source=Provenance.UNKNOWN,
                    sensitive=False,
                )
                for field in typed
            ]

        request: LLMRequest[AnswerMappingOutput] = LLMRequest(
            task=TASK_ANSWER_MAPPER,
            system=ANSWER_MAPPER_SYSTEM,
            blocks=[
                answer_mapper_user_message(
                    fields_listing=answer_mapping.format_fields_for_prompt(typed),
                    facts_listing=answer_mapping.format_facts_for_prompt(facts, evidence),
                )
            ],
            output_model=AnswerMappingOutput,
            temperature=0.0,
            max_tokens=4000,
            allowed_evidence_ids=frozenset(evidence),
        )

        try:
            result = self._llm.run(request)
        except DomainError as exc:
            logger.warning(
                "application.answer_mapping_failed",
                extra={"application_id": row.id, "code": exc.code},
            )
            return [
                ApplicationAnswer(
                    id=new_id("application_answer"),
                    application_id=row.id,
                    field=field.name,
                    question=field.label or field.name,
                    source=Provenance.UNKNOWN,
                )
                for field in typed
            ]

        by_name = {field.name: field for field in typed}
        mapped: list[ApplicationAnswer] = []
        answered: set[str] = set()

        for proposal in result.output.answers:
            field = by_name.get(proposal.field_name)
            if field is None:
                continue  # the model invented a field; ignore it
            answered.add(field.name)

            error = (
                None
                if proposal.needs_user
                else answer_mapping.validate_answer(field, proposal.answer)
            )
            usable = not proposal.needs_user and error is None

            mapped.append(
                ApplicationAnswer(
                    id=new_id("application_answer"),
                    application_id=row.id,
                    field=field.name,
                    question=field.label or field.name,
                    answer=proposal.answer if usable else "",
                    # Model output is an AI suggestion until the user confirms it,
                    # never a fact — that distinction drives the UI badge.
                    source=Provenance.AI if usable else Provenance.UNKNOWN,
                    confidence=proposal.confidence if usable else 0.0,
                    user_verified=False,
                    sensitive=False,
                )
            )

        for name, field in by_name.items():
            if name not in answered:
                mapped.append(
                    ApplicationAnswer(
                        id=new_id("application_answer"),
                        application_id=row.id,
                        field=name,
                        question=field.label or name,
                        source=Provenance.UNKNOWN,
                    )
                )
        return mapped

    def _draft_cover_letter(
        self, row: ApplicationRow, facts: list[CandidateFact], evidence: dict[str, Evidence]
    ) -> None:
        if self._llm is None:
            return

        job_row = self._session.get(JobRow, row.job_id)
        if job_row is None:  # pragma: no cover - FK guarantees this
            return
        job = row_to_job(job_row)

        request: LLMRequest[CoverLetterOutput] = LLMRequest(
            task=TASK_COVER_LETTER,
            system=COVER_LETTER_SYSTEM,
            blocks=[
                cover_letter_user_message(
                    job_title=job.title,
                    company=job.company,
                    requirements=[item.text for item in job.requirements],
                    facts_listing=answer_mapping.format_facts_for_prompt(facts, evidence),
                ),
                UntrustedContent(
                    label=f"{job.source}:{job.source_job_id}", text=job.description[:6000]
                ),
            ],
            output_model=CoverLetterOutput,
            max_tokens=3000,
            allowed_evidence_ids=frozenset(evidence),
        )

        try:
            result = self._llm.run(request)
        except DomainError as exc:
            logger.warning(
                "application.cover_letter_failed",
                extra={"application_id": row.id, "code": exc.code},
            )
            return

        directory = self._settings.applications_dir / row.id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / "cover_letter.txt"
        path.write_text(result.output.body, encoding="utf-8")

        artifact = ArtifactRow(
            id=new_id("artifact"),
            type=ArtifactType.COVER_LETTER,
            path=str(path),
            sha256=sha256_bytes(result.output.body.encode("utf-8")),
            application_id=row.id,
        )
        self._session.add(artifact)
        self._session.flush()
        row.cover_letter_artifact_id = artifact.id

        audit.record(
            self._session,
            actor=audit.ACTOR_SYSTEM,
            action="application.cover_letter_drafted",
            entity_type="application",
            entity_id=row.id,
            metadata={
                "artifact_id": artifact.id,
                "omitted_claims": len(result.output.omitted_claims),
                "prompt_version": COVER_LETTER_VERSION,
            },
        )

    # -------------------------------------------------------------------- review

    def set_answer(
        self, application_id: str, field: str, answer: str, *, verified: bool = True
    ) -> Application:
        """Record a user's answer. This is how a sensitive field gets filled."""
        row = self._row(application_id)
        if row.status in (
            ApplicationStatus.SUBMITTED,
            ApplicationStatus.SUBMITTING,
        ):
            raise ConflictError(f"cannot edit answers while the application is {row.status.value}")

        target = next((item for item in row.answers if item.field == field), None)
        if target is None:
            raise NotFoundError(f"field {field!r} is not part of this application")

        target.answer = answer
        target.source = Provenance.USER
        target.confidence = 1.0
        target.user_verified = verified
        audit.record(
            self._session,
            actor=audit.ACTOR_USER,
            action="application.answer_provided",
            entity_type="application",
            entity_id=row.id,
            # The value itself is deliberately not audited: these are the most
            # sensitive strings in the system.
            metadata={"field": field, "sensitive": target.sensitive},
        )
        self._session.flush()
        return self._to_schema(row)

    def attach_resume(self, application_id: str, resume_id: str) -> Application:
        row = self._row(application_id)
        resume = self._session.get(ResumeRow, resume_id)
        if resume is None or resume.candidate_id != row.candidate_id:
            raise NotFoundError(f"resume {resume_id} not found for this candidate")
        row.approved_resume_id = resume_id
        self._session.flush()
        return self._to_schema(row)

    def checklist(self, application_id: str) -> list[str]:
        """Pre-submit checklist (FR-53): what the user still has to resolve.

        The pending-approval state is deliberately excluded. It is not a user task —
        it is the very action the checklist unlocks, and listing it would leave the UI
        showing an item that can never be ticked off before approving.
        ``claim_submission`` still checks the full blocker list, approval included.
        """
        blockers = self._to_schema(self._row(application_id)).submission_blockers()
        return [item for item in blockers if not item.startswith("status is")]

    def approve(self, application_id: str) -> Application:
        """The explicit human approval that submission requires (FR-54).

        Refuses while any blocker remains, so approval cannot be given to an
        application that is not actually ready.
        """
        row = self._row(application_id)

        # Check the transition first: a stopped or submitted application cannot be
        # approved however clean its checklist looks, and reporting "not ready"
        # for a terminal state would point the user at the wrong problem.
        if ApplicationStatus.USER_APPROVED not in APPLICATION_TRANSITIONS[row.status]:
            raise ConflictError(
                f"cannot move application from {row.status.value} to user_approved",
                details={
                    "current": row.status.value,
                    "requested": ApplicationStatus.USER_APPROVED.value,
                    "allowed": sorted(item.value for item in APPLICATION_TRANSITIONS[row.status]),
                },
            )

        application = self._to_schema(row)
        blockers = [
            item for item in application.submission_blockers() if not item.startswith("status is")
        ]
        if blockers:
            raise ValidationFailed(
                "application is not ready for approval",
                details={"blockers": blockers},
            )

        self._transition(row, ApplicationStatus.USER_APPROVED, actor=audit.ACTOR_USER)
        audit.record(
            self._session,
            actor=audit.ACTOR_USER,
            action="application.approved",
            entity_type="application",
            entity_id=row.id,
            metadata={"resume_id": row.approved_resume_id},
        )
        return self._to_schema(row)

    # ------------------------------------------------------------------- submit

    def claim_submission(self, application_id: str) -> str:
        """Claim the right to submit exactly once, returning the idempotency key.

        Called immediately before an attempt. The key is unique in the database, so a
        second attempt — including one after a network timeout left the first
        outcome unknown — cannot claim it again.
        """
        row = self._row(application_id)

        # The idempotency check comes first, and deliberately so: once a key exists an
        # attempt has been made, and that is the answer the caller needs — reporting
        # the intermediate SUBMITTING state instead would read as "not approved yet"
        # and invite a retry, which is precisely what this guard exists to prevent.
        if row.idempotency_key is not None:
            raise ConflictError(
                "a submission has already been attempted for this application",
                details={"application_id": row.id, "status": row.status.value},
            )

        if row.status is not ApplicationStatus.USER_APPROVED:
            raise ConflictError(
                f"submission requires an approved application, this one is {row.status.value}",
                details={"status": row.status.value},
            )
        if not self._settings.allow_browser_submit:
            raise SafetyStop(
                "submission is disabled by configuration (CA_ALLOW_BROWSER_SUBMIT=false)",
                reason=StopReason.USER_REQUESTED.value,
                details={"application_id": row.id},
            )
        blockers = self._to_schema(row).submission_blockers()
        if blockers:
            raise ValidationFailed(
                "pre-submit checklist is not clear", details={"blockers": blockers}
            )

        row.idempotency_key = new_id("application")[-32:]
        self._transition(row, ApplicationStatus.SUBMITTING, actor=audit.ACTOR_USER)
        return row.idempotency_key

    def record_submitted(
        self, application_id: str, *, confirmation_ref: str | None = None
    ) -> Application:
        row = self._row(application_id)
        self._transition(row, ApplicationStatus.SUBMITTED, actor=audit.ACTOR_SYSTEM)
        row.submitted_at = datetime.now(UTC)
        row.confirmation_ref = confirmation_ref
        audit.record(
            self._session,
            actor=audit.ACTOR_SYSTEM,
            action="application.submitted",
            entity_type="application",
            entity_id=row.id,
            metadata={"confirmation_ref": confirmation_ref},
        )
        self._session.flush()
        return self._to_schema(row)

    def record_stop(
        self,
        application_id: str,
        *,
        reason: StopReason,
        detail: str,
        status: ApplicationStatus = ApplicationStatus.STOPPED,
    ) -> Application:
        """Record a safe stop: what happened and what the user must decide.

        Used for CAPTCHA, unexpected authentication, suspicious pages and uncertain
        network state. The application is parked, never silently retried.
        """
        row = self._row(application_id)
        self._transition(row, status, actor=audit.ACTOR_SYSTEM)
        row.stop_reason = reason.value
        row.stop_detail = detail
        audit.record(
            self._session,
            actor=audit.ACTOR_SYSTEM,
            action="application.stopped",
            entity_type="application",
            entity_id=row.id,
            metadata={"reason": reason.value, "status": status.value},
        )
        self._session.flush()
        logger.warning(
            "application.stopped",
            extra={"application_id": row.id, "reason": reason.value},
        )
        return self._to_schema(row)
