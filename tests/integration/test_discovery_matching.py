"""Discovery -> dedupe -> matching, end to end against the fixture postings."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.db.models import AuditEvent
from packages.core.llm.base import LLMRequest
from packages.core.llm.client import LLMClient
from packages.core.llm.stub import StubProvider
from packages.core.settings import Settings
from packages.schemas.candidate import CandidateProfile
from packages.schemas.enums import Eligibility, FactCategory, Provenance, RemoteMode
from packages.schemas.job import JobSearchCriteria
from packages.schemas.llm_tasks import TASK_MATCH_EXPLAINER, TASK_RESUME_EXTRACTOR
from services.candidate.service import CandidateService
from services.jobs.base import SourceRegistry
from services.jobs.service import JobService
from services.jobs.sources import LocalFixtureSource
from services.matching.service import MatchService
from tests.support import extractors

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture
def job_service(db: Session, settings: Settings) -> JobService:
    """A service wired to the fixture postings only — no network."""
    target = settings.data_dir / "jobs" / "fixtures.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "jobs.json", target)

    registry = SourceRegistry()
    registry.register(LocalFixtureSource(target))
    return JobService(db, registry, settings)


@pytest.fixture
def profile(
    db: Session,
    settings: Settings,
    stub_provider: StubProvider,
    llm: LLMClient,
    sample_tex_path: Path,
) -> CandidateProfile:
    """An ingested candidate with preferences set for the fixture postings."""
    stub_provider.register(TASK_RESUME_EXTRACTOR, extractors.well_behaved)
    service = CandidateService(db, llm, settings)
    candidate = service.create_candidate(display_name="Match Candidate")
    service.ingest_resume(candidate.id, sample_tex_path)

    from packages.schemas.candidate import CandidatePreferences, TargetRole

    service.update_preferences(
        candidate.id,
        CandidatePreferences(
            target_roles=[
                TargetRole(
                    title="Senior Machine Learning Engineer",
                    seniority="senior",
                    keywords=["computer vision", "pytorch"],
                )
            ],
            locations=["Bengaluru", "Remote (India)"],
            remote_modes=[RemoteMode.REMOTE, RemoteMode.HYBRID],
            exclusions=["gambling"],
        ),
    )
    # Verify the facts so scoring treats them as usable candidate truth.
    for fact in service.get_profile(candidate.id).facts:
        service.verify_fact(candidate.id, fact.id)
    db.commit()
    return service.get_profile(candidate.id)


class TestDiscovery:
    def test_discovery_stores_normalized_jobs(self, job_service: JobService) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        assert len(jobs) == 6
        assert all(job.source == "local" for job in jobs)
        assert all(job.retrieved_at is not None for job in jobs)

    def test_requirements_are_extracted_on_ingest(self, job_service: JobService) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        ml_job = next(job for job in jobs if job.source_job_id == "req-ml-001")
        assert ml_job.requirements
        keys = {req.key for req in ml_job.requirements}
        assert "python" in keys
        assert "years_experience>=4" in keys

    def test_benefits_do_not_become_requirements(self, job_service: JobService) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        ml_job = next(job for job in jobs if job.source_job_id == "req-ml-001")
        texts = " | ".join(req.text for req in ml_job.requirements)
        assert "health insurance" not in texts
        assert "30 days of leave" not in texts

    def test_cross_posted_role_is_grouped(self, job_service: JobService) -> None:
        """The repost shares a canonical URL and requisition id with the original."""
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        original = next(job for job in jobs if job.source_job_id == "req-ml-001")
        repost = next(job for job in jobs if job.source_job_id == "req-ml-001-repost")
        assert original.dedupe_group == repost.dedupe_group

    def test_feed_collapses_duplicates(self, job_service: JobService) -> None:
        job_service.discover(JobSearchCriteria(limit=50))
        collapsed = job_service.list_jobs(collapse_duplicates=True)
        expanded = job_service.list_jobs(collapse_duplicates=False)
        assert len(collapsed) == len(expanded) - 1

    def test_duplicates_are_discoverable_from_a_job(self, job_service: JobService) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        original = next(job for job in jobs if job.source_job_id == "req-ml-001")
        assert len(job_service.duplicates_of(original.id)) == 1

    def test_rediscovery_updates_rather_than_duplicating(self, job_service: JobService) -> None:
        first = job_service.discover(JobSearchCriteria(limit=50))
        second = job_service.discover(JobSearchCriteria(limit=50))
        assert {job.id for job in first} == {job.id for job in second}
        assert job_service.count() == 6

    def test_title_criteria_filter(self, job_service: JobService) -> None:
        jobs = job_service.discover(JobSearchCriteria(titles=["Backend Engineer"], limit=50))
        assert jobs
        assert all("backend" in job.title.lower() for job in jobs)

    def test_discovery_is_audited(self, job_service: JobService, db: Session) -> None:
        job_service.discover(JobSearchCriteria(limit=50))
        events = db.scalars(select(AuditEvent).where(AuditEvent.action == "jobs.discovered")).all()
        assert len(events) == 1
        assert events[0].metadata_json["created"] == 6

    def test_source_health_is_reported(self, job_service: JobService) -> None:
        health = job_service.source_health()
        assert health[0].source == "local"
        assert health[0].healthy is True


class TestMatching:
    def test_relevant_role_outranks_irrelevant_one(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        matches = MatchService(db).match_many(profile, jobs)

        ranked = {match.job_id: match.score for match in matches}
        ml_job = next(job for job in jobs if job.source_job_id == "req-ml-001")
        vp_job = next(job for job in jobs if job.source_job_id == "req-vp-002")
        assert ranked[ml_job.id] > ranked[vp_job.id]

    def test_excluded_company_is_ineligible(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        gambling = next(job for job in jobs if job.source_job_id == "req-gamble-009")
        match = MatchService(db).match(profile, gambling, explain=False)
        assert match.eligibility is Eligibility.INELIGIBLE
        assert any("gambling" in reason for reason in match.hard_constraints.blocking)

    def test_score_is_reconstructable(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        match = MatchService(db).match(profile, jobs[0], explain=False)
        assert match.recompute_score() == pytest.approx(match.score, abs=1e-6)

    def test_strengths_cite_evidence(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        ml_job = next(job for job in jobs if job.source_job_id == "req-ml-001")
        match = MatchService(db).match(profile, ml_job, explain=False)
        assert match.strengths
        assert all(item.evidence for item in match.strengths)

    def test_unstated_authorization_appears_as_uncertainty_not_rejection(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        vp_job = next(job for job in jobs if job.source_job_id == "req-vp-002")
        match = MatchService(db).match(profile, vp_job, explain=False)
        # Blocked on years, but authorization must be an unknown, never an assumed no.
        assert any("work authorization" in item for item in match.uncertainty)

    def test_match_is_idempotent_per_candidate_job(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        service = MatchService(db)
        first = service.match(profile, jobs[0], explain=False)
        second = service.match(profile, jobs[0], explain=False)
        assert first.id == second.id
        assert service.list_for_candidate(profile.id, limit=100)

    def test_weights_are_recorded_with_the_match(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        match = MatchService(db).match(profile, jobs[0], explain=False)
        assert sum(match.weights_used.values()) == pytest.approx(1.0)

    def test_matching_is_audited_with_components(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        MatchService(db).match(profile, jobs[0], explain=False)
        event = db.scalars(select(AuditEvent).where(AuditEvent.action == "match.scored")).first()
        assert event is not None
        assert "hard_constraints" in event.metadata_json["components"]


class TestPromptInjectionThroughAJobDescription:
    """The fixture posting tries to hijack the system. It must fail completely."""

    def test_injected_instructions_are_fenced_and_neutralised(
        self,
        db: Session,
        job_service: JobService,
        profile: CandidateProfile,
        stub_provider: StubProvider,
        llm: LLMClient,
    ) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        hostile = next(job for job in jobs if job.source_job_id == "req-injection-777")

        captured: list[str] = []

        def explainer(request: LLMRequest[Any]) -> dict[str, Any]:
            captured.append(request.render_user_message())
            return {
                "explanation": "Scored on the supplied strengths only.",
                "cited_evidence_ids": [],
            }

        stub_provider.register(TASK_MATCH_EXPLAINER, explainer)
        MatchService(db, llm).match(profile, hostile, explain=True)

        rendered = captured[0]
        # The posting's forged closing tag must not have escaped the fence.
        assert rendered.count("</untrusted_content>") == 1
        assert "&lt;/untrusted_content&gt;" in rendered
        assert '<untrusted_content source="local:req-injection-777">' in rendered

    def test_injection_does_not_create_candidate_facts(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        """The posting claims 20 years of Rust and a PhD. Neither may appear."""
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        hostile = next(job for job in jobs if job.source_job_id == "req-injection-777")
        MatchService(db).match(profile, hostile, explain=False)

        service = CandidateService(db)
        claims = " | ".join(fact.claim.lower() for fact in service.get_profile(profile.id).facts)
        assert "rust" not in claims
        assert "phd" not in claims
        assert "stanford" not in claims

    def test_injection_cannot_grant_work_authorization(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        """It asserts "requires no visa sponsorship". Authorization stays unknown."""
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        hostile = next(job for job in jobs if job.source_job_id == "req-injection-777")
        MatchService(db).match(profile, hostile, explain=False)

        service = CandidateService(db)
        auth = [
            fact
            for fact in service.get_profile(profile.id).facts
            if fact.category is FactCategory.WORK_AUTHORIZATION
        ]
        assert all(fact.provenance is not Provenance.RESUME for fact in auth)

    def test_injected_text_does_not_inflate_the_score(
        self, db: Session, job_service: JobService, profile: CandidateProfile
    ) -> None:
        jobs = job_service.discover(JobSearchCriteria(limit=50))
        hostile = next(job for job in jobs if job.source_job_id == "req-injection-777")
        match = MatchService(db).match(profile, hostile, explain=False)
        # Score comes only from weighted components, which the posting cannot touch.
        assert match.recompute_score() == pytest.approx(match.score, abs=1e-6)
        assert match.score <= 1.0
