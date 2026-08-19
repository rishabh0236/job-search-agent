"""Schema invariants — the guardrails that must hold regardless of any LLM."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from packages.schemas.application import Application, ApplicationAnswer
from packages.schemas.common import Claim, EvidenceRef
from packages.schemas.enums import ApplicationStatus, Provenance, SourceType
from packages.schemas.matching import JobMatch, ScoreComponent, ScoreWeights
from packages.schemas.resume import Resume


def _evidence() -> EvidenceRef:
    return EvidenceRef(
        evidence_id="ev_1",
        source_id="res_1",
        locator="page=1;line=4",
        quote="Led migration of the billing service to Kubernetes.",
    )


class TestClaimTrust:
    def test_unknown_provenance_is_never_usable(self) -> None:
        claim = Claim(text="Fluent in Japanese", provenance=Provenance.UNKNOWN)
        assert claim.is_supported is False
        assert claim.trust_label() == "Unknown / requires confirmation"

    def test_resume_claim_needs_evidence_to_be_usable(self) -> None:
        unsupported = Claim(text="Ran a team of 12", provenance=Provenance.RESUME)
        assert unsupported.is_supported is False

        supported = Claim(
            text="Ran a team of 12", provenance=Provenance.RESUME, evidence=[_evidence()]
        )
        assert supported.is_supported is True

    def test_user_provided_is_its_own_evidence(self) -> None:
        claim = Claim(text="Available from 1 October", provenance=Provenance.USER)
        assert claim.is_supported is True
        assert claim.trust_label() == "User-provided information"

    def test_ai_suggestion_is_labelled_as_such(self) -> None:
        claim = Claim(
            text="Experienced in distributed systems",
            provenance=Provenance.AI,
            evidence=[_evidence()],
        )
        assert claim.trust_label() == "AI suggestion"

    def test_verified_resume_fact_is_labelled_verified(self) -> None:
        claim = Claim(
            text="Senior Engineer at Acme",
            provenance=Provenance.RESUME,
            evidence=[_evidence()],
            verified=True,
        )
        assert claim.trust_label() == "Verified candidate fact"


class TestScoreWeights:
    def test_defaults_sum_to_one(self) -> None:
        assert pytest.approx(sum(ScoreWeights().as_dict().values()), abs=1e-9) == 1.0

    def test_weights_that_do_not_sum_to_one_are_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"must sum to 1\.0"):
            ScoreWeights(hard_constraints=0.9, required_skill_evidence=0.9)

    def test_score_is_recomputable_from_components(self) -> None:
        match = JobMatch(
            id="match_1",
            job_id="job_1",
            candidate_id="cand_1",
            score=0.0,
            components=[
                ScoreComponent(name="hard_constraints", raw_score=1.0, weight=0.30),
                ScoreComponent(name="required_skill_evidence", raw_score=0.5, weight=0.25),
            ],
        )
        # A stored score must always be re-derivable; this is what protects the
        # UI from displaying a number no component supports.
        assert match.recompute_score() == pytest.approx(0.425)


class TestApplicationStateMachine:
    def test_happy_path_transitions_are_allowed(self) -> None:
        app = Application(id="app_1", candidate_id="cand_1", job_id="job_1")
        assert app.can_transition_to(ApplicationStatus.PREPARING)

    def test_cannot_skip_review_and_submit(self) -> None:
        app = Application(id="app_1", candidate_id="cand_1", job_id="job_1")
        assert app.can_transition_to(ApplicationStatus.SUBMITTING) is False

    def test_submitted_is_terminal(self) -> None:
        app = Application(
            id="app_1",
            candidate_id="cand_1",
            job_id="job_1",
            status=ApplicationStatus.SUBMITTED,
        )
        assert all(not app.can_transition_to(target) for target in ApplicationStatus)

    def test_submission_blocked_without_approved_resume(self) -> None:
        app = Application(
            id="app_1",
            candidate_id="cand_1",
            job_id="job_1",
            status=ApplicationStatus.USER_APPROVED,
        )
        assert "no approved resume attached" in app.submission_blockers()

    def test_submission_blocked_by_unconfirmed_sensitive_answer(self) -> None:
        app = Application(
            id="app_1",
            candidate_id="cand_1",
            job_id="job_1",
            status=ApplicationStatus.USER_APPROVED,
            approved_resume_id="res_2",
            answers=[
                ApplicationAnswer(
                    id="ans_1",
                    application_id="app_1",
                    field="requires_visa_sponsorship",
                    source=Provenance.AI,
                    sensitive=True,
                )
            ],
        )
        blockers = app.submission_blockers()
        assert blockers == ["answer requires confirmation: requires_visa_sponsorship"]

    def test_verified_answers_clear_the_checklist(self) -> None:
        app = Application(
            id="app_1",
            candidate_id="cand_1",
            job_id="job_1",
            status=ApplicationStatus.USER_APPROVED,
            approved_resume_id="res_2",
            answers=[
                ApplicationAnswer(
                    id="ans_1",
                    application_id="app_1",
                    field="requires_visa_sponsorship",
                    answer="No",
                    source=Provenance.USER,
                    sensitive=True,
                    user_verified=True,
                )
            ],
        )
        assert app.submission_blockers() == []

    def test_already_submitted_cannot_submit_again(self) -> None:
        app = Application(
            id="app_1",
            candidate_id="cand_1",
            job_id="job_1",
            status=ApplicationStatus.USER_APPROVED,
            approved_resume_id="res_2",
            submitted_at=datetime.now(UTC),
        )
        assert "already submitted" in app.submission_blockers()


class TestResumeImmutability:
    def test_original_cannot_be_derived(self) -> None:
        with pytest.raises(ValidationError, match="cannot be derived"):
            Resume(
                id="res_1",
                candidate_id="cand_1",
                source_type=SourceType.LATEX,
                source_path="data/resumes/original.tex",
                sha256="a" * 64,
                version=1,
                is_original=True,
                derived_from_id="res_0",
                created_at=datetime.now(UTC),
            )

    def test_tailored_version_must_record_its_parent(self) -> None:
        with pytest.raises(ValidationError, match="must record derived_from_id"):
            Resume(
                id="res_2",
                candidate_id="cand_1",
                source_type=SourceType.LATEX,
                source_path="data/resumes/tailored.tex",
                sha256="b" * 64,
                version=2,
                is_original=False,
                created_at=datetime.now(UTC),
            )
