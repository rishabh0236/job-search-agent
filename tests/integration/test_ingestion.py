"""End-to-end ingestion: file -> evidence -> validated facts.

Includes the golden test for M1: a fixed resume plus a well-behaved extractor must
produce a known, evidence-linked profile.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.db.models import AuditEvent, CandidateFact, Evidence, Resume
from packages.core.errors import ConflictError
from packages.core.llm.client import LLMClient
from packages.core.llm.stub import StubProvider
from packages.schemas.enums import FactCategory, Provenance, SourceType
from packages.schemas.llm_tasks import TASK_RESUME_EXTRACTOR
from services.candidate.service import CandidateService
from tests.support import extractors


@pytest.fixture
def service(db: Session, llm: LLMClient) -> CandidateService:
    return CandidateService(db, llm)


class TestDeterministicOnlyIngestion:
    """With no extractor fixture registered, the model call fails by design.

    This is the "no API key" path: it must still produce a usable profile.
    """

    def test_ingestion_succeeds_without_a_working_model(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path
    ) -> None:
        report = service.ingest_resume(candidate_id, sample_tex_path)

        assert report.llm_extraction_ran is False
        assert report.facts_created > 0
        assert any(f.code == "llm_extraction_failed" for f in report.findings)

    def test_contacts_and_skills_are_extracted_deterministically(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path, db: Session
    ) -> None:
        service.ingest_resume(candidate_id, sample_tex_path)
        profile = service.get_profile(candidate_id)

        contacts = {fact.claim for fact in profile.facts_by_category(FactCategory.CONTACT)}
        assert "priya.raghavan@example.com" in contacts
        assert "github.com/praghavan" in contacts

        skills = {fact.claim for fact in profile.facts_by_category(FactCategory.SKILL)}
        assert {"Python", "Go", "SQL", "FastAPI", "PyTorch", "Kubernetes"} <= skills

    def test_every_fact_carries_resolvable_evidence(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path
    ) -> None:
        service.ingest_resume(candidate_id, sample_tex_path)
        profile = service.get_profile(candidate_id)

        for fact in profile.facts:
            if fact.provenance is Provenance.UNKNOWN:
                continue
            assert fact.evidence, f"fact without evidence: {fact.claim}"
            for ref in fact.evidence:
                assert ref.quote
                assert ref.locator


class TestGoldenIngestion:
    """Fixed resume + well-behaved extractor -> known profile."""

    @pytest.fixture
    def service(self, db: Session, stub_provider: StubProvider, llm: LLMClient) -> CandidateService:
        stub_provider.register(TASK_RESUME_EXTRACTOR, extractors.well_behaved)
        return CandidateService(db, llm)

    def test_report_matches_the_expected_shape(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path
    ) -> None:
        report = service.ingest_resume(candidate_id, sample_tex_path)

        assert report.llm_extraction_ran is True
        assert report.is_original is True
        assert report.source_type is SourceType.LATEX
        assert report.sections == ["summary", "experience", "projects", "skills", "education"]
        assert report.facts_rejected == 0
        assert report.evidence_count == report.block_count
        # Deterministic contacts/skills plus model-proposed roles and achievements.
        assert report.facts_created >= 20

    def test_achievements_are_extracted_with_their_metric_intact(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path
    ) -> None:
        service.ingest_resume(candidate_id, sample_tex_path)
        profile = service.get_profile(candidate_id)

        achievements = profile.facts_by_category(FactCategory.ACHIEVEMENT)
        latency = next(fact for fact in achievements if "p99 latency" in fact.claim)
        # The supported figure survives; the guard only rejects unsupported ones.
        assert "35%" in latency.claim
        assert latency.evidence[0].quote.startswith("Led the redesign")

    def test_experience_facts_reference_the_role_entry(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path
    ) -> None:
        service.ingest_resume(candidate_id, sample_tex_path)
        profile = service.get_profile(candidate_id)

        experience = profile.facts_by_category(FactCategory.EXPERIENCE)
        claims = " | ".join(fact.claim for fact in experience)
        assert "Senior Machine Learning Engineer" in claims
        assert "Backend Engineer" in claims

    def test_nothing_is_verified_until_a_human_says_so(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path
    ) -> None:
        service.ingest_resume(candidate_id, sample_tex_path)
        profile = service.get_profile(candidate_id)

        assert all(fact.verified is False for fact in profile.facts)
        assert profile.unresolved_count == len(profile.facts)

    def test_ingestion_is_idempotent_at_the_fact_level(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path, db: Session
    ) -> None:
        """Re-ingesting the same content must not duplicate the profile.

        The file itself is rejected as a duplicate, so this drives the second pass
        through a copy with identical content under a different name.
        """
        service.ingest_resume(candidate_id, sample_tex_path)
        first_count = db.scalar(
            select(CandidateFact)
            .where(CandidateFact.candidate_id == candidate_id)
            .exists()
            .select()
        )
        assert first_count

        facts_before = len(service.get_profile(candidate_id).facts)
        evidence_before = len(
            db.scalars(select(Evidence).where(Evidence.candidate_id == candidate_id)).all()
        )

        # Second copy: same bytes, different filename and a non-original version.
        copy_path = sample_tex_path.parent / "resume_sample_copy.tex"
        try:
            copy_path.write_bytes(sample_tex_path.read_bytes())
            with pytest.raises(ConflictError, match="already been ingested"):
                service.ingest_resume(candidate_id, copy_path)
        finally:
            copy_path.unlink(missing_ok=True)

        assert len(service.get_profile(candidate_id).facts) == facts_before
        assert (
            len(db.scalars(select(Evidence).where(Evidence.candidate_id == candidate_id)).all())
            == evidence_before
        )


class TestSafetyDuringIngestion:
    """Hallucinated proposals must be rejected, not stored."""

    def _ingest_with(
        self,
        behaviour: object,
        db: Session,
        stub_provider: StubProvider,
        llm: LLMClient,
        candidate_id: str,
        path: Path,
    ) -> tuple[CandidateService, object]:
        stub_provider.register(TASK_RESUME_EXTRACTOR, behaviour)  # type: ignore[arg-type]
        service = CandidateService(db, llm)
        report = service.ingest_resume(candidate_id, path)
        return service, report

    def test_fabricated_evidence_id_never_reaches_the_profile(
        self,
        db: Session,
        stub_provider: StubProvider,
        llm: LLMClient,
        candidate_id: str,
        sample_tex_path: Path,
    ) -> None:
        service, report = self._ingest_with(
            extractors.fabricating_evidence_id,
            db,
            stub_provider,
            llm,
            candidate_id,
            sample_tex_path,
        )
        profile = service.get_profile(candidate_id)
        assert not any("Globex" in fact.claim for fact in profile.facts)
        # The evidence allowlist in the LLM client rejects the whole response, which
        # is reported as a failed extraction rather than a silent drop.
        assert any(
            f.code in ("llm_extraction_failed", "fabricated_evidence")
            for f in report.findings  # type: ignore[attr-defined]
        )

    def test_invented_metric_is_rejected(
        self,
        db: Session,
        stub_provider: StubProvider,
        llm: LLMClient,
        candidate_id: str,
        sample_tex_path: Path,
    ) -> None:
        service, report = self._ingest_with(
            extractors.fabricating_metric, db, stub_provider, llm, candidate_id, sample_tex_path
        )
        profile = service.get_profile(candidate_id)

        assert not any("250%" in fact.claim for fact in profile.facts)
        assert report.facts_rejected >= 1  # type: ignore[attr-defined]
        assert any(f.code == "unsupported_metric" for f in report.findings)  # type: ignore[attr-defined]

    def test_invented_employer_is_rejected(
        self,
        db: Session,
        stub_provider: StubProvider,
        llm: LLMClient,
        candidate_id: str,
        sample_tex_path: Path,
    ) -> None:
        service, report = self._ingest_with(
            extractors.fabricating_employer, db, stub_provider, llm, candidate_id, sample_tex_path
        )
        profile = service.get_profile(candidate_id)
        assert not any(
            fact.attributes.get("employer") == "Initech Global" for fact in profile.facts
        )
        assert any(f.code == "unsupported_entity" for f in report.findings)  # type: ignore[attr-defined]

    def test_unattributed_observation_becomes_unknown(
        self,
        db: Session,
        stub_provider: StubProvider,
        llm: LLMClient,
        candidate_id: str,
        sample_tex_path: Path,
    ) -> None:
        service, report = self._ingest_with(
            extractors.unattributed_observation,
            db,
            stub_provider,
            llm,
            candidate_id,
            sample_tex_path,
        )
        profile = service.get_profile(candidate_id)

        leadership = next(fact for fact in profile.facts if fact.claim == "Leadership")
        assert leadership.provenance is Provenance.UNKNOWN
        assert leadership.confidence == 0.0
        assert any(f.code == "model_uncertain" for f in report.findings)  # type: ignore[attr-defined]


class TestResumeImmutability:
    def test_stored_original_is_read_only_and_content_addressed(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path, db: Session
    ) -> None:
        report = service.ingest_resume(candidate_id, sample_tex_path)
        resume = db.get(Resume, report.resume_id)
        assert resume is not None

        stored = Path(resume.source_path)
        assert stored.exists()
        assert report.sha256[:16] in stored.name
        # Read-only on disk: the original is never rewritten (CLAUDE.md rule 6).
        assert stored.stat().st_mode & 0o222 == 0

    def test_second_original_is_refused(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path, tmp_path: Path
    ) -> None:
        service.ingest_resume(candidate_id, sample_tex_path)

        variant = tmp_path / "other.tex"
        variant.write_text(sample_tex_path.read_text() + "\n% distinct content\n")
        with pytest.raises(ConflictError, match="already has an immutable original"):
            service.ingest_resume(candidate_id, variant, is_original=True)

    def test_versions_increment(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path, tmp_path: Path
    ) -> None:
        first = service.ingest_resume(candidate_id, sample_tex_path)

        variant = tmp_path / "v2.tex"
        variant.write_text(sample_tex_path.read_text() + "\n% v2\n")
        second = service.ingest_resume(candidate_id, variant)

        assert first.is_original is True
        assert second.is_original is False


class TestAuditing:
    def test_ingestion_is_audited(
        self, service: CandidateService, candidate_id: str, sample_tex_path: Path, db: Session
    ) -> None:
        report = service.ingest_resume(candidate_id, sample_tex_path)

        events = db.scalars(select(AuditEvent).where(AuditEvent.action == "resume.ingested")).all()
        assert len(events) == 1
        assert events[0].entity_id == report.resume_id
        assert events[0].metadata_json["facts_created"] == report.facts_created
        assert events[0].metadata_json["prompt_version"]
