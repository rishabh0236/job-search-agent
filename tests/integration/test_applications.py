"""Application preparation, the review loop, and the submission gate.

The tests that matter most here are the refusals: an application must not become
submittable while anything is unconfirmed, and it must never be submittable twice.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.db.models import AuditEvent
from packages.core.db.models import Job as JobRow
from packages.core.errors import ConflictError, SafetyStop, ValidationFailed
from packages.core.llm.base import LLMRequest
from packages.core.llm.client import LLMClient
from packages.core.llm.stub import StubProvider
from packages.core.settings import Settings
from packages.schemas.application import FormField, FormSpec
from packages.schemas.enums import ApplicationStatus, Provenance, StopReason
from packages.schemas.llm_tasks import (
    TASK_ANSWER_MAPPER,
    TASK_COVER_LETTER,
    TASK_RESUME_EXTRACTOR,
)
from services.application.service import ApplicationService
from services.candidate.service import CandidateService
from tests.support import extractors

JOB_ID = "job_app_1"


def _form() -> FormSpec:
    """Mirrors the mock site's form, including its sensitive fields."""
    return FormSpec(
        url="http://localhost:8001/jobs/mock-001/apply",
        submit_selector="#submit-application",
        fields=[
            FormField(name="full_name", label="Full name", required=True),
            FormField(name="email", label="Email address", field_type="email", required=True),
            FormField(name="phone", label="Phone number", field_type="tel"),
            FormField(name="location", label="Current location"),
            FormField(name="resume", label="Resume (PDF)", field_type="file", required=True),
            FormField(name="cover_letter", label="Cover letter", field_type="textarea"),
            FormField(
                name="work_authorization",
                label="Are you authorised to work in this country?",
                field_type="select",
                required=True,
                options=["yes", "no", "sponsorship"],
            ),
            FormField(name="notice_period", label="Notice period (days)", field_type="number"),
            FormField(
                name="why_this_role", label="Why do you want this role?", field_type="textarea"
            ),
            FormField(
                name="terms",
                label="I confirm the information provided is accurate",
                field_type="checkbox",
                required=True,
            ),
        ],
    )


@pytest.fixture
def prepared_candidate(
    db: Session,
    settings: Settings,
    stub_provider: StubProvider,
    llm: LLMClient,
    sample_tex_path: Path,
) -> tuple[str, str]:
    """A verified candidate and a stored job. Returns (candidate_id, resume_id)."""
    stub_provider.register(TASK_RESUME_EXTRACTOR, extractors.well_behaved)
    service = CandidateService(db, llm, settings)
    candidate = service.create_candidate(display_name="Priya Raghavan")
    report = service.ingest_resume(candidate.id, sample_tex_path)

    for fact in service.get_profile(candidate.id).facts:
        service.verify_fact(candidate.id, fact.id)

    db.add(
        JobRow(
            id=JOB_ID,
            source="local",
            source_job_id="app-1",
            company="Northwind Retail Analytics",
            title="Senior Machine Learning Engineer",
            description="Requirements\n- Strong PyTorch\n",
            retrieved_at=datetime.now(UTC),
        )
    )
    db.commit()
    return candidate.id, report.resume_id


def _answer_mapper(request: LLMRequest[Any]) -> dict[str, Any]:
    """A well-behaved mapper: answers what it can, defers the rest."""
    return {
        "answers": [
            {
                "field_name": "why_this_role",
                "answer": "I build retail computer vision systems and this role is the same work.",
                "evidence_ids": [],
                "confidence": 0.7,
                "needs_user": False,
                "reason": "",
            }
        ]
    }


def _cover_letter(request: LLMRequest[Any]) -> dict[str, Any]:
    return {
        "body": "Dear Hiring Team,\n\nI lead machine learning work on retail shelf "
        "recognition and would like to bring that to Northwind.\n\nPriya",
        "cited_evidence_ids": [],
        "omitted_claims": ["Candidate may have managed a team of ten"],
    }


@pytest.fixture
def service(
    db: Session, settings: Settings, stub_provider: StubProvider, llm: LLMClient
) -> ApplicationService:
    stub_provider.register(TASK_ANSWER_MAPPER, _answer_mapper)
    stub_provider.register(TASK_COVER_LETTER, _cover_letter)
    return ApplicationService(db, llm, settings)


class TestLifecycle:
    def test_create_starts_in_created(
        self, service: ApplicationService, prepared_candidate: tuple[str, str]
    ) -> None:
        candidate_id, _ = prepared_candidate
        application = service.create(candidate_id, JOB_ID)
        assert application.status is ApplicationStatus.CREATED

    def test_duplicate_application_is_refused(
        self, service: ApplicationService, prepared_candidate: tuple[str, str]
    ) -> None:
        candidate_id, _ = prepared_candidate
        service.create(candidate_id, JOB_ID)
        with pytest.raises(ConflictError, match="already exists"):
            service.create(candidate_id, JOB_ID)

    def test_prepare_moves_to_review(
        self, service: ApplicationService, prepared_candidate: tuple[str, str]
    ) -> None:
        candidate_id, resume_id = prepared_candidate
        application = service.create(candidate_id, JOB_ID)
        prepared = service.prepare(application.id, _form(), approved_resume_id=resume_id)
        assert prepared.status is ApplicationStatus.READY_FOR_REVIEW

    def test_preparing_twice_is_idempotent_not_additive(
        self, service: ApplicationService, prepared_candidate: tuple[str, str]
    ) -> None:
        candidate_id, resume_id = prepared_candidate
        application = service.create(candidate_id, JOB_ID)
        first = service.prepare(application.id, _form(), approved_resume_id=resume_id)
        # Re-preparing from review is allowed and must not duplicate answers.
        second = service.prepare(application.id, _form(), approved_resume_id=resume_id)
        assert len(second.answers) == len(first.answers)


class TestAnswerMapping:
    @pytest.fixture
    def prepared(self, service: ApplicationService, prepared_candidate: tuple[str, str]) -> Any:
        candidate_id, resume_id = prepared_candidate
        application = service.create(candidate_id, JOB_ID)
        return service.prepare(application.id, _form(), approved_resume_id=resume_id)

    def test_contact_fields_are_filled_deterministically(self, prepared: Any) -> None:
        by_field = {answer.field: answer for answer in prepared.answers}
        assert by_field["email"].answer == "priya.raghavan@example.com"
        assert by_field["email"].source is Provenance.RESUME
        assert by_field["email"].user_verified is True
        assert "98450" in by_field["phone"].answer

    def test_name_is_filled_from_the_profile(self, prepared: Any) -> None:
        by_field = {answer.field: answer for answer in prepared.answers}
        assert by_field["full_name"].answer == "Priya Raghavan"

    def test_file_fields_are_not_answered_here(self, prepared: Any) -> None:
        """The runner attaches the exact approved artifact; nothing guesses a file."""
        assert "resume" not in {answer.field for answer in prepared.answers}

    def test_sensitive_fields_are_left_for_the_user(self, prepared: Any) -> None:
        by_field = {answer.field: answer for answer in prepared.answers}
        for field in ("work_authorization", "notice_period"):
            assert by_field[field].sensitive is True
            assert by_field[field].answer == ""
            assert by_field[field].needs_user is True

    def test_model_answers_are_labelled_as_suggestions(self, prepared: Any) -> None:
        by_field = {answer.field: answer for answer in prepared.answers}
        assert by_field["why_this_role"].source is Provenance.AI
        assert by_field["why_this_role"].user_verified is False
        assert by_field["why_this_role"].needs_user is True

    def test_terms_checkbox_needs_the_users_own_tick(self, prepared: Any) -> None:
        by_field = {answer.field: answer for answer in prepared.answers}
        assert by_field["terms"].needs_user is True

    def test_cover_letter_is_drafted_and_stored(
        self, prepared: Any, db: Session, settings: Settings
    ) -> None:
        assert prepared.cover_letter_artifact_id is not None
        from packages.core.db.models import Artifact

        artifact = db.get(Artifact, prepared.cover_letter_artifact_id)
        assert artifact is not None
        assert Path(artifact.path).read_text().startswith("Dear Hiring Team")

    def test_omitted_claims_are_audited_rather_than_silently_dropped(
        self, prepared: Any, db: Session
    ) -> None:
        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "application.cover_letter_drafted")
        ).one()
        assert event.metadata_json["omitted_claims"] == 1


class TestSubmissionGate:
    @pytest.fixture
    def ready_for_review(
        self, service: ApplicationService, prepared_candidate: tuple[str, str]
    ) -> str:
        candidate_id, resume_id = prepared_candidate
        application = service.create(candidate_id, JOB_ID)
        service.prepare(application.id, _form(), approved_resume_id=resume_id)
        return application.id

    def test_checklist_blocks_while_sensitive_answers_are_unconfirmed(
        self, service: ApplicationService, ready_for_review: str
    ) -> None:
        blockers = service.checklist(ready_for_review)
        assert any("work_authorization" in item for item in blockers)
        assert any("terms" in item for item in blockers)

    def test_approval_is_refused_while_blockers_remain(
        self, service: ApplicationService, ready_for_review: str
    ) -> None:
        with pytest.raises(ValidationFailed, match="not ready for approval"):
            service.approve(ready_for_review)

    def _clear_blockers(self, service: ApplicationService, application_id: str) -> None:
        application = service.get(application_id)
        for answer in application.unresolved_answers:
            service.set_answer(application_id, answer.field, "yes")

    def test_checklist_clears_once_the_user_answers(
        self, service: ApplicationService, ready_for_review: str
    ) -> None:
        self._clear_blockers(service, ready_for_review)
        assert service.checklist(ready_for_review) == []

    def test_user_answers_are_labelled_user_provided(
        self, service: ApplicationService, ready_for_review: str
    ) -> None:
        service.set_answer(ready_for_review, "work_authorization", "yes")
        answer = next(
            item
            for item in service.get(ready_for_review).answers
            if item.field == "work_authorization"
        )
        assert answer.source is Provenance.USER
        assert answer.user_verified is True

    def test_answer_values_are_never_written_to_the_audit_log(
        self, service: ApplicationService, ready_for_review: str, db: Session
    ) -> None:
        """Salary and authorization answers are the most sensitive strings we hold."""
        service.set_answer(ready_for_review, "notice_period", "90")
        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "application.answer_provided")
        ).one()
        assert "90" not in str(event.metadata_json)
        assert event.metadata_json["field"] == "notice_period"

    def test_cannot_submit_without_approval(
        self, service: ApplicationService, ready_for_review: str, settings: Settings
    ) -> None:
        self._clear_blockers(service, ready_for_review)
        with pytest.raises(ConflictError, match="requires an approved application"):
            service.claim_submission(ready_for_review)

    def test_submission_is_blocked_by_the_kill_switch(
        self,
        service: ApplicationService,
        ready_for_review: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Even a fully approved application respects CA_ALLOW_BROWSER_SUBMIT."""
        self._clear_blockers(service, ready_for_review)
        service.approve(ready_for_review)

        with pytest.raises(SafetyStop, match="disabled by configuration"):
            service.claim_submission(ready_for_review)

    def test_approved_application_can_claim_submission_once(
        self,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ready_for_review: str,
        service: ApplicationService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._clear_blockers(service, ready_for_review)
        service.approve(ready_for_review)

        monkeypatch.setattr(settings, "allow_browser_submit", True)
        enabled = ApplicationService(db, llm, settings)

        key = enabled.claim_submission(ready_for_review)
        assert key
        assert enabled.get(ready_for_review).status is ApplicationStatus.SUBMITTING

        # The second claim is the duplicate-submit guard.
        with pytest.raises(ConflictError, match="already been attempted"):
            enabled.claim_submission(ready_for_review)

    def test_submitted_is_terminal_and_recorded(
        self,
        db: Session,
        settings: Settings,
        llm: LLMClient,
        ready_for_review: str,
        service: ApplicationService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._clear_blockers(service, ready_for_review)
        service.approve(ready_for_review)
        monkeypatch.setattr(settings, "allow_browser_submit", True)

        enabled = ApplicationService(db, llm, settings)
        enabled.claim_submission(ready_for_review)
        submitted = enabled.record_submitted(ready_for_review, confirmation_ref="MOCK-ABC123")

        assert submitted.status is ApplicationStatus.SUBMITTED
        assert submitted.confirmation_ref == "MOCK-ABC123"
        assert submitted.submitted_at is not None
        assert "already submitted" in submitted.submission_blockers()

    def test_answers_cannot_be_edited_after_submission(
        self,
        db: Session,
        settings: Settings,
        llm: LLMClient,
        ready_for_review: str,
        service: ApplicationService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        self._clear_blockers(service, ready_for_review)
        service.approve(ready_for_review)
        monkeypatch.setattr(settings, "allow_browser_submit", True)
        enabled = ApplicationService(db, llm, settings)
        enabled.claim_submission(ready_for_review)
        enabled.record_submitted(ready_for_review, confirmation_ref="X")

        with pytest.raises(ConflictError, match="cannot edit answers"):
            enabled.set_answer(ready_for_review, "notice_period", "30")


class TestSafeStops:
    def test_a_captcha_stop_is_recorded_with_its_reason(
        self, service: ApplicationService, prepared_candidate: tuple[str, str], db: Session
    ) -> None:
        candidate_id, resume_id = prepared_candidate
        application = service.create(candidate_id, JOB_ID)
        service.prepare(application.id, _form(), approved_resume_id=resume_id)

        stopped = service.record_stop(
            application.id,
            reason=StopReason.CAPTCHA,
            detail="A CAPTCHA widget appeared on the application form",
        )
        assert stopped.status is ApplicationStatus.STOPPED
        assert stopped.stop_reason is StopReason.CAPTCHA
        assert "CAPTCHA" in stopped.stop_detail

        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "application.stopped")
        ).one()
        assert event.metadata_json["reason"] == "captcha"

    def test_stopped_is_terminal(
        self, service: ApplicationService, prepared_candidate: tuple[str, str]
    ) -> None:
        candidate_id, resume_id = prepared_candidate
        application = service.create(candidate_id, JOB_ID)
        service.prepare(application.id, _form(), approved_resume_id=resume_id)
        service.record_stop(application.id, reason=StopReason.CAPTCHA, detail="stopped")

        with pytest.raises(ConflictError, match="cannot move application"):
            service.approve(application.id)


class TestStateMachine:
    def test_illegal_transition_is_refused_with_the_allowed_set(
        self, service: ApplicationService, prepared_candidate: tuple[str, str], db: Session
    ) -> None:
        candidate_id, _ = prepared_candidate
        application = service.create(candidate_id, JOB_ID)

        with pytest.raises(ConflictError) as exc:
            service.record_submitted(application.id)
        assert exc.value.details["current"] == "created"
        assert "preparing" in exc.value.details["allowed"]

    def test_every_transition_is_audited(
        self, service: ApplicationService, prepared_candidate: tuple[str, str], db: Session
    ) -> None:
        candidate_id, resume_id = prepared_candidate
        application = service.create(candidate_id, JOB_ID)
        service.prepare(application.id, _form(), approved_resume_id=resume_id)

        events = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "application.status_changed")
        ).all()
        transitions = [(e.metadata_json["from"], e.metadata_json["to"]) for e in events]
        assert ("created", "preparing") in transitions
        assert ("preparing", "ready_for_review") in transitions
