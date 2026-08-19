"""The application runner driven against the live mock site.

Uses the HTTP driver so the whole state machine — including every safe stop — is
exercised without a browser. The Playwright driver implements the same protocol, so
what is verified here is the logic that governs a real run.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core.db.models import Artifact, AuditEvent
from packages.core.db.models import Job as JobRow
from packages.core.errors import ConflictError, SafetyStop
from packages.core.llm.client import LLMClient
from packages.core.llm.stub import StubProvider
from packages.core.settings import Settings
from packages.schemas.enums import ApplicationStatus, ArtifactType, StopReason
from packages.schemas.llm_tasks import TASK_ANSWER_MAPPER, TASK_COVER_LETTER, TASK_RESUME_EXTRACTOR
from services.application.service import ApplicationService
from services.browser.driver import HttpFormDriver
from services.browser.mock_site.app import app as mock_app
from services.browser.runner import ApplicationRunner
from services.candidate.service import CandidateService
from tests.support import extractors

MOCK_PORT = 8199
MOCK_BASE = f"http://127.0.0.1:{MOCK_PORT}"


@pytest.fixture(scope="module")
def mock_site() -> Iterator[str]:
    """Run the mock site in-process for the module."""
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=MOCK_PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(100):
        time.sleep(0.05)
        try:
            httpx.get(f"{MOCK_BASE}/", timeout=1.0)
            break
        except httpx.HTTPError:
            continue
    else:  # pragma: no cover
        pytest.skip("mock site did not start")

    yield MOCK_BASE
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture
def clean_site(mock_site: str) -> str:
    httpx.post(f"{mock_site}/_reset", timeout=5.0)
    return mock_site


@pytest.fixture
def setup(
    db: Session,
    settings: Settings,
    stub_provider: StubProvider,
    llm: LLMClient,
    sample_tex_path: Path,
    sample_pdf_path: Path,
) -> tuple[str, str, str]:
    """A verified candidate with a compiled PDF, a job, and an application.

    Returns (candidate_id, resume_id, application_id).
    """
    stub_provider.register(TASK_RESUME_EXTRACTOR, extractors.well_behaved)
    stub_provider.register(
        TASK_ANSWER_MAPPER,
        lambda request: {
            "answers": [
                {
                    "field_name": "why_this_role",
                    "answer": "I work on retail computer vision systems.",
                    "evidence_ids": [],
                    "confidence": 0.7,
                    "needs_user": False,
                    "reason": "",
                }
            ]
        },
    )
    stub_provider.register(
        TASK_COVER_LETTER,
        lambda request: {
            "body": "Dear team, I would like to apply.",
            "cited_evidence_ids": [],
            "omitted_claims": [],
        },
    )

    candidates = CandidateService(db, llm, settings)
    candidate = candidates.create_candidate(display_name="Priya Raghavan")
    report = candidates.ingest_resume(candidate.id, sample_tex_path)
    for fact in candidates.get_profile(candidate.id).facts:
        candidates.verify_fact(candidate.id, fact.id)

    # The runner uploads a compiled PDF sitting beside the .tex, never the source.
    from packages.core.db.models import Resume as ResumeRow

    resume = db.get(ResumeRow, report.resume_id)
    assert resume is not None
    Path(resume.source_path).with_suffix(".pdf").write_bytes(sample_pdf_path.read_bytes())

    db.add(
        JobRow(
            id="job_runner_1",
            source="local",
            source_job_id="mock-001",
            company="Northwind Retail Analytics",
            title="Senior Machine Learning Engineer",
            description="Requirements\n- Strong PyTorch\n",
            retrieved_at=datetime.now(UTC),
        )
    )
    db.flush()

    application = ApplicationService(db, llm, settings).create(candidate.id, "job_runner_1")
    db.commit()
    return candidate.id, report.resume_id, application.id


@pytest.fixture
def runner(db: Session, settings: Settings, llm: LLMClient) -> Iterator[ApplicationRunner]:
    driver = HttpFormDriver(snapshot_dir=settings.browser_dir / "test")
    yield ApplicationRunner(db, driver, llm, settings)
    driver.close()


class TestFormDiscovery:
    def test_form_fields_are_discovered_by_semantic_handles(
        self, runner: ApplicationRunner, setup: tuple[str, str, str], clean_site: str
    ) -> None:
        _, resume_id, application_id = setup
        result = runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-001/apply", approved_resume_id=resume_id
        )

        assert result.stopped is False
        assert result.form is not None
        names = {field.name for field in result.form.fields}
        assert {"full_name", "email", "work_authorization", "resume", "terms"} <= names
        # Labels were read from <label for=...>, not positions.
        labels = {field.name: field.label for field in result.form.fields}
        assert "Full name" in labels["full_name"]

    def test_select_options_are_captured(
        self, runner: ApplicationRunner, setup: tuple[str, str, str], clean_site: str
    ) -> None:
        _, resume_id, application_id = setup
        result = runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-001/apply", approved_resume_id=resume_id
        )
        assert result.form is not None
        auth = next(f for f in result.form.fields if f.name == "work_authorization")
        assert "sponsorship" in auth.options

    def test_preparation_stops_for_review_and_lists_pending_fields(
        self, runner: ApplicationRunner, setup: tuple[str, str, str], clean_site: str
    ) -> None:
        _, resume_id, application_id = setup
        result = runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-001/apply", approved_resume_id=resume_id
        )

        assert result.application.status is ApplicationStatus.READY_FOR_REVIEW
        assert "email" in result.filled_fields
        assert "work_authorization" in result.pending_fields
        assert "terms" in result.pending_fields

    def test_discovery_is_audited(
        self,
        runner: ApplicationRunner,
        setup: tuple[str, str, str],
        clean_site: str,
        db: Session,
    ) -> None:
        _, resume_id, application_id = setup
        runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-001/apply", approved_resume_id=resume_id
        )
        event = db.scalars(
            select(AuditEvent).where(AuditEvent.action == "runner.form_discovered")
        ).one()
        assert event.metadata_json["driver"] == "http"


class TestCaptchaStop:
    def test_a_captcha_page_stops_the_run_with_an_explanation(
        self,
        db: Session,
        settings: Settings,
        llm: LLMClient,
        runner: ApplicationRunner,
        setup: tuple[str, str, str],
        clean_site: str,
    ) -> None:
        """mock-003 renders a CAPTCHA. The run must stop, not attempt anything."""
        _, resume_id, application_id = setup
        result = runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-003/apply", approved_resume_id=resume_id
        )

        assert result.stopped is True
        assert result.stop_reason is StopReason.CAPTCHA
        assert result.application.status is ApplicationStatus.STOPPED
        assert "complete it yourself" in result.stop_detail

    def test_the_stop_is_snapshotted_for_diagnosis(
        self, runner: ApplicationRunner, setup: tuple[str, str, str], clean_site: str
    ) -> None:
        _, resume_id, application_id = setup
        result = runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-003/apply", approved_resume_id=resume_id
        )
        assert result.snapshot_path is not None
        assert Path(result.snapshot_path).is_file()

    def test_a_stopped_application_cannot_be_submitted(
        self, runner: ApplicationRunner, setup: tuple[str, str, str], clean_site: str
    ) -> None:
        _, resume_id, application_id = setup
        runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-003/apply", approved_resume_id=resume_id
        )
        with pytest.raises(ConflictError):
            runner.submit(application_id, f"{clean_site}/jobs/mock-003/apply")


class TestSubmission:
    def _approve(
        self, db: Session, settings: Settings, llm: LLMClient, application_id: str
    ) -> ApplicationService:
        service = ApplicationService(db, llm, settings)
        for answer in service.get(application_id).unresolved_answers:
            service.set_answer(
                application_id,
                answer.field,
                "yes" if answer.field in ("work_authorization", "terms") else "60",
            )
        service.approve(application_id)
        return service

    def test_submission_requires_the_kill_switch(
        self,
        db: Session,
        settings: Settings,
        llm: LLMClient,
        runner: ApplicationRunner,
        setup: tuple[str, str, str],
        clean_site: str,
    ) -> None:
        _, resume_id, application_id = setup
        runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-001/apply", approved_resume_id=resume_id
        )
        self._approve(db, settings, llm, application_id)

        with pytest.raises(SafetyStop, match="disabled by configuration"):
            runner.submit(application_id, f"{clean_site}/jobs/mock-001/apply")

    def test_full_run_submits_and_records_the_confirmation(
        self,
        db: Session,
        settings: Settings,
        llm: LLMClient,
        setup: tuple[str, str, str],
        clean_site: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, resume_id, application_id = setup
        driver = HttpFormDriver(snapshot_dir=settings.browser_dir / "run")
        runner = ApplicationRunner(db, driver, llm, settings)

        runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-001/apply", approved_resume_id=resume_id
        )
        self._approve(db, settings, llm, application_id)
        monkeypatch.setattr(settings, "allow_browser_submit", True)

        result = ApplicationRunner(db, driver, llm, settings).submit(
            application_id, f"{clean_site}/jobs/mock-001/apply"
        )

        assert result.stopped is False
        assert result.application.status is ApplicationStatus.SUBMITTED
        assert result.confirmation_ref is not None
        assert result.confirmation_ref.startswith("MOCK-")
        driver.close()

    def test_the_site_received_the_approved_pdf_and_the_users_own_answers(
        self,
        db: Session,
        settings: Settings,
        llm: LLMClient,
        setup: tuple[str, str, str],
        clean_site: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, resume_id, application_id = setup
        driver = HttpFormDriver()
        runner = ApplicationRunner(db, driver, llm, settings)
        runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-001/apply", approved_resume_id=resume_id
        )
        self._approve(db, settings, llm, application_id)
        monkeypatch.setattr(settings, "allow_browser_submit", True)

        result = ApplicationRunner(db, driver, llm, settings).submit(
            application_id, f"{clean_site}/jobs/mock-001/apply"
        )
        received = httpx.get(
            f"{clean_site}/submissions/{result.confirmation_ref}", timeout=5
        ).json()

        assert received["found"] is True
        submission = received["submission"]
        assert submission["email"] == "priya.raghavan@example.com"
        assert submission["work_authorization"] == "yes"
        assert submission["resume_filename"].endswith(".pdf")
        driver.close()

    def test_a_confirmation_snapshot_is_stored_as_an_artifact(
        self,
        db: Session,
        settings: Settings,
        llm: LLMClient,
        setup: tuple[str, str, str],
        clean_site: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _, resume_id, application_id = setup
        driver = HttpFormDriver()
        runner = ApplicationRunner(db, driver, llm, settings)
        runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-001/apply", approved_resume_id=resume_id
        )
        self._approve(db, settings, llm, application_id)
        monkeypatch.setattr(settings, "allow_browser_submit", True)
        ApplicationRunner(db, driver, llm, settings).submit(
            application_id, f"{clean_site}/jobs/mock-001/apply"
        )

        artifact = db.scalars(
            select(Artifact).where(Artifact.type == ArtifactType.PAGE_SNAPSHOT)
        ).one()
        assert Path(artifact.path).is_file()
        # The snapshot must not contain the candidate's typed answers.
        assert "priya.raghavan@example.com" not in Path(artifact.path).read_text()
        driver.close()

    def test_a_second_submission_is_refused(
        self,
        db: Session,
        settings: Settings,
        llm: LLMClient,
        setup: tuple[str, str, str],
        clean_site: str,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The idempotency key is claimed before typing, so a retry cannot duplicate."""
        _, resume_id, application_id = setup
        driver = HttpFormDriver()
        runner = ApplicationRunner(db, driver, llm, settings)
        runner.prepare_run(
            application_id, f"{clean_site}/jobs/mock-001/apply", approved_resume_id=resume_id
        )
        self._approve(db, settings, llm, application_id)
        monkeypatch.setattr(settings, "allow_browser_submit", True)

        second_runner = ApplicationRunner(db, driver, llm, settings)
        second_runner.submit(application_id, f"{clean_site}/jobs/mock-001/apply")

        with pytest.raises(ConflictError, match="already been attempted"):
            second_runner.submit(application_id, f"{clean_site}/jobs/mock-001/apply")
        driver.close()
