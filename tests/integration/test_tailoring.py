"""The tailoring loop: propose -> validate -> patch -> compile -> version.

These tests compile real PDFs with the vendored tectonic, so they exercise the whole
FR-30..FR-37 path rather than a mocked approximation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.db.models import AuditEvent
from packages.core.db.models import Job as JobRow
from packages.core.db.models import Resume as ResumeRow
from packages.core.llm.client import LLMClient
from packages.core.llm.stub import StubProvider
from packages.core.settings import Settings
from packages.schemas.enums import EmploymentType, RemoteMode, RequirementKind, TailoringMode
from packages.schemas.job import Job, JobRequirement
from packages.schemas.llm_tasks import TASK_RESUME_EDITOR, TASK_RESUME_EXTRACTOR
from packages.schemas.resume import TailoringResult
from services.candidate.service import CandidateService
from services.resume.compiler import TectonicCompiler
from services.resume.tailoring import TailoringService
from tests.support import extractors

REPO_ROOT = Path(__file__).resolve().parents[2]


def _job_row(db: Session, job: Job) -> None:
    """Persist the job.

    A tailored resume records the job it was tailored for, and that column is a real
    foreign key — in the product the job always comes from discovery, so the test
    reflects that rather than weakening the constraint.
    """
    db.add(
        JobRow(
            id=job.id,
            source=job.source,
            source_job_id=job.source_job_id,
            company=job.company,
            title=job.title,
            location=job.location,
            remote=job.remote.value,
            employment_type=job.employment_type.value,
            description=job.description,
            requirements_json={
                "items": [item.model_dump(mode="json") for item in job.requirements]
            },
            retrieved_at=job.retrieved_at,
        )
    )
    db.flush()


def _job() -> Job:
    return Job(
        id="job_tailor_1",
        source="local",
        source_job_id="tailor-1",
        company="Northwind Retail Analytics",
        title="Senior Machine Learning Engineer",
        location="Bengaluru, India",
        remote=RemoteMode.HYBRID,
        employment_type=EmploymentType.FULL_TIME,
        description="Requirements\n- Mentoring engineers\n- Strong PyTorch\n",
        requirements=[
            JobRequirement(text="Mentoring engineers", kind=RequirementKind.REQUIRED),
            JobRequirement(text="Strong PyTorch", kind=RequirementKind.REQUIRED, key="pytorch"),
        ],
        retrieved_at=datetime.now(UTC),
    )


@pytest.fixture
def compiler() -> TectonicCompiler:
    binary = REPO_ROOT / ".tooling" / "bin" / "tectonic"
    if not binary.exists():
        pytest.skip("tectonic not installed; run scripts/bootstrap.sh")
    return TectonicCompiler(binary=binary, timeout_seconds=180)


@pytest.fixture
def ingested(
    db: Session,
    settings: Settings,
    stub_provider: StubProvider,
    llm: LLMClient,
    sample_tex_path: Path,
) -> tuple[str, str]:
    """Returns (candidate_id, original_resume_id)."""
    stub_provider.register(TASK_RESUME_EXTRACTOR, extractors.well_behaved)
    service = CandidateService(db, llm, settings)
    candidate = service.create_candidate(display_name="Tailoring Candidate")
    report = service.ingest_resume(candidate.id, sample_tex_path)
    _job_row(db, _job())
    db.commit()
    return candidate.id, report.resume_id


class TestAstAndSource:
    def test_ast_is_built_from_the_stored_original(
        self, db: Session, settings: Settings, llm: LLMClient, ingested: tuple[str, str]
    ) -> None:
        _, resume_id = ingested
        service = TailoringService(db, llm, settings)
        ast = service.build_ast(resume_id)
        assert ast.sections
        assert any(section.kind == "bullet" for section in ast.sections)


class TestFaithfulTailoring:
    def test_produces_a_new_version_and_leaves_the_original_untouched(
        self,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ingested: tuple[str, str],
        compiler: TectonicCompiler,
    ) -> None:
        candidate_id, resume_id = ingested
        stub_provider.register(TASK_RESUME_EDITOR, extractors.faithful_editor)
        service = TailoringService(db, llm, settings, compiler)

        original_source = service.load_source(resume_id)
        result = service.tailor(candidate_id, resume_id, _job(), mode=TailoringMode.BALANCED)

        assert result.compile_result is not None
        assert result.compile_result.success is True
        assert result.blocked is False
        assert result.resume_id != resume_id

        # The original file on disk is byte-identical.
        assert service.load_source(resume_id) == original_source

        new_row = db.get(ResumeRow, result.resume_id)
        assert new_row is not None
        assert new_row.is_original is False
        assert new_row.derived_from_id == resume_id
        assert new_row.version > 1

    def test_the_edit_is_present_in_the_new_version(
        self,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ingested: tuple[str, str],
        compiler: TectonicCompiler,
    ) -> None:
        candidate_id, resume_id = ingested
        stub_provider.register(TASK_RESUME_EDITOR, extractors.faithful_editor)
        service = TailoringService(db, llm, settings, compiler)
        result = service.tailor(candidate_id, resume_id, _job())

        tailored = service.load_source(result.resume_id)
        assert "as they joined the vision team" in tailored
        # And the template survived intact.
        assert "\\newcommand{\\resumeItem}" in tailored

    def test_compiled_pdf_keeps_its_page_count(
        self,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ingested: tuple[str, str],
        compiler: TectonicCompiler,
    ) -> None:
        candidate_id, resume_id = ingested
        stub_provider.register(TASK_RESUME_EDITOR, extractors.faithful_editor)
        result = TailoringService(db, llm, settings, compiler).tailor(
            candidate_id, resume_id, _job()
        )

        assert result.compile_result is not None
        assert result.compile_result.page_count == 1
        assert not any(f.code == "content_lost" for f in result.findings)

    def test_diff_is_available_for_review(
        self,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ingested: tuple[str, str],
        compiler: TectonicCompiler,
    ) -> None:
        candidate_id, resume_id = ingested
        stub_provider.register(TASK_RESUME_EDITOR, extractors.faithful_editor)
        service = TailoringService(db, llm, settings, compiler)
        result = service.tailor(candidate_id, resume_id, _job())

        diff = service.diff(result.resume_id)
        assert len(diff) == 1
        assert "Mentored three engineers" in diff[0]["before"]
        assert "vision team" in diff[0]["after"]
        assert diff[0]["rationale"]

    def test_tailoring_is_audited(
        self,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ingested: tuple[str, str],
        compiler: TectonicCompiler,
    ) -> None:
        candidate_id, resume_id = ingested
        stub_provider.register(TASK_RESUME_EDITOR, extractors.faithful_editor)
        TailoringService(db, llm, settings, compiler).tailor(candidate_id, resume_id, _job())

        event = db.scalars(select(AuditEvent).where(AuditEvent.action == "resume.tailored")).one()
        assert event.metadata_json["applied"] == 1
        assert event.metadata_json["new_version_created"] is True
        assert event.metadata_json["prompt_version"]


class TestFactualityGuards:
    """A tailored resume must never assert something the evidence does not."""

    def _tailor_with(
        self,
        behaviour: object,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ingested: tuple[str, str],
        compiler: TectonicCompiler,
    ) -> tuple[TailoringService, str, TailoringResult]:
        candidate_id, resume_id = ingested
        stub_provider.register(TASK_RESUME_EDITOR, behaviour)  # type: ignore[arg-type]
        service = TailoringService(db, llm, settings, compiler)
        return service, resume_id, service.tailor(candidate_id, resume_id, _job())

    def test_invented_metric_is_rejected_and_no_version_is_created(
        self,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ingested: tuple[str, str],
        compiler: TectonicCompiler,
    ) -> None:
        service, resume_id, result = self._tailor_with(
            extractors.metric_inventing_editor, db, settings, stub_provider, llm, ingested, compiler
        )
        assert any(f.code == "unsupported_metric" for f in result.findings)
        assert result.resume_id == resume_id  # no new version
        assert "40%" not in service.load_source(resume_id)

    def test_latex_injection_in_a_replacement_is_rejected(
        self,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ingested: tuple[str, str],
        compiler: TectonicCompiler,
    ) -> None:
        """\\input would read a file off the machine at compile time."""
        service, resume_id, result = self._tailor_with(
            extractors.latex_injecting_editor, db, settings, stub_provider, llm, ingested, compiler
        )
        assert any(f.code in ("forbidden_command", "unknown_command") for f in result.findings)
        assert "\\input" not in service.load_source(resume_id)

    def test_stale_proposal_is_rejected(
        self,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ingested: tuple[str, str],
        compiler: TectonicCompiler,
    ) -> None:
        _, _, result = self._tailor_with(
            extractors.stale_editor, db, settings, stub_provider, llm, ingested, compiler
        )
        assert any(f.code == "stale_old_text" for f in result.findings)

    def test_unaddressed_requirements_are_surfaced_not_invented(
        self,
        db: Session,
        settings: Settings,
        stub_provider: StubProvider,
        llm: LLMClient,
        ingested: tuple[str, str],
        compiler: TectonicCompiler,
    ) -> None:
        _, _, result = self._tailor_with(
            extractors.stale_editor, db, settings, stub_provider, llm, ingested, compiler
        )
        assert any(
            f.code == "unaddressed_requirement" and "Kubernetes" in f.message
            for f in result.findings
        )


class TestNoModelAvailable:
    def test_tailoring_without_a_model_proposes_nothing_and_says_so(
        self, db: Session, settings: Settings, ingested: tuple[str, str], compiler: TectonicCompiler
    ) -> None:
        candidate_id, resume_id = ingested
        result = TailoringService(db, None, settings, compiler).tailor(
            candidate_id, resume_id, _job()
        )

        assert result.edits == []
        assert result.resume_id == resume_id
        assert any(f.code == "llm_unavailable" for f in result.findings)
