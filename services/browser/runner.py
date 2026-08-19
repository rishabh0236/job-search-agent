"""The application runner.

Drives one application through a real form, obeying skills/06 and skills/07:

    open -> assess safety -> discover form -> map fields -> fill verified answers
         -> ask about unknowns -> validate -> STOP for review
         -> [explicit user approval] -> claim submission -> submit -> verify -> record

Two things it will never do, enforced structurally rather than by convention:

* **Submit without approval.** ``prepare_run`` stops at review. Submission lives in a
  separate method that calls ``ApplicationService.claim_submission``, which refuses
  unless the application is already ``USER_APPROVED`` with a clear checklist.
* **Work around a control.** Any CAPTCHA, login wall, payment request or
  access-denied page ends the run through ``record_stop``, with the reason and a
  snapshot. There is no retry loop and no bypass path to find.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy.orm import Session

from packages.core import audit
from packages.core.db.models import Artifact as ArtifactRow
from packages.core.db.models import Resume as ResumeRow
from packages.core.errors import NotFoundError, SafetyStop
from packages.core.ids import new_id
from packages.core.llm.client import LLMClient
from packages.core.logging import get_logger
from packages.core.settings import Settings, get_settings
from packages.schemas.application import Application, FormSpec
from packages.schemas.enums import ApplicationStatus, ArtifactType, StopReason
from services.application.service import ApplicationService
from services.browser import safety
from services.browser.driver import BrowserDriver, PageState
from services.candidate.extraction import sha256_file

logger = get_logger(__name__)


@dataclass(slots=True)
class RunResult:
    """What a run did, for the Apply Runner screen."""

    application: Application
    form: FormSpec | None = None
    stopped: bool = False
    stop_reason: StopReason | None = None
    stop_detail: str = ""
    snapshot_path: str | None = None
    filled_fields: list[str] = field(default_factory=list)
    pending_fields: list[str] = field(default_factory=list)
    confirmation_ref: str | None = None


class ApplicationRunner:
    def __init__(
        self,
        session: Session,
        driver: BrowserDriver,
        llm: LLMClient | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._driver = driver
        self._settings = settings or get_settings()
        self._applications = ApplicationService(session, llm, self._settings)

    # ------------------------------------------------------------------- helpers

    def _stop(
        self,
        application_id: str,
        verdict: safety.SafetyVerdict,
        *,
        status: ApplicationStatus = ApplicationStatus.STOPPED,
    ) -> RunResult:
        """Record a safe stop with a snapshot, and explain it."""
        snapshot = self._driver.snapshot(
            f"stop-{verdict.reason.value if verdict.reason else 'unknown'}"
        )
        reason = verdict.reason or StopReason.SUSPICIOUS_PAGE
        application = self._applications.record_stop(
            application_id, reason=reason, detail=verdict.detail, status=status
        )
        logger.warning(
            "runner.stopped",
            extra={
                "application_id": application_id,
                "reason": reason.value,
                "driver": self._driver.name,
            },
        )
        return RunResult(
            application=application,
            stopped=True,
            stop_reason=reason,
            stop_detail=verdict.detail,
            snapshot_path=snapshot,
        )

    def _snapshot_dir(self, application_id: str) -> Path:
        return self._settings.browser_dir / application_id

    # ------------------------------------------------------------------- prepare

    def prepare_run(
        self,
        application_id: str,
        url: str,
        *,
        approved_resume_id: str | None = None,
    ) -> RunResult:
        """Open the form, map answers, and stop for human review.

        This method can never submit. That is not a policy note — there is no code
        path from here to a POST of the application form.
        """
        # Validates that the application exists before touching the network.
        self._applications.get(application_id)

        state = self._driver.open(url)
        verdict = state.verdict()
        if not verdict.safe:
            return self._stop(application_id, verdict)

        if state.status_code >= 400:
            return self._stop(
                application_id,
                safety.SafetyVerdict(
                    safe=False,
                    reason=StopReason.UNEXPECTED_STRUCTURE,
                    detail=f"the application page returned HTTP {state.status_code}",
                ),
            )

        form = self._driver.discover_form()
        if not form.fields:
            return self._stop(
                application_id,
                safety.SafetyVerdict(
                    safe=False,
                    reason=StopReason.UNEXPECTED_STRUCTURE,
                    detail=(
                        "no form fields were found on the page; the site may render its "
                        "form with JavaScript, which this driver does not execute"
                    ),
                ),
            )

        prepared = self._applications.prepare(
            application_id, form, approved_resume_id=approved_resume_id
        )

        audit.record(
            self._session,
            actor=audit.ACTOR_SYSTEM,
            action="runner.form_discovered",
            entity_type="application",
            entity_id=application_id,
            metadata={"url": url, "fields": len(form.fields), "driver": self._driver.name},
        )

        return RunResult(
            application=prepared,
            form=form,
            filled_fields=[a.field for a in prepared.answers if a.answer and not a.needs_user],
            pending_fields=[a.field for a in prepared.unresolved_answers],
        )

    # -------------------------------------------------------------------- submit

    def submit(self, application_id: str, url: str) -> RunResult:
        """Fill and submit an already-approved application.

        ``claim_submission`` is called *before* anything is typed: it enforces the
        approval requirement, the clear checklist and the once-only guarantee, and it
        raises rather than returning a value the caller might ignore.
        """
        idempotency_key = self._applications.claim_submission(application_id)
        application = self._applications.get(application_id)

        state = self._driver.open(url)
        verdict = state.verdict()
        if not verdict.safe:
            # A control appeared between review and submission. Nothing typed, and the
            # claimed key stays consumed so this cannot be retried blindly.
            return self._stop(application_id, verdict, status=ApplicationStatus.STOPPED)

        form = self._driver.discover_form()
        answers = {answer.field: answer for answer in application.answers}

        filled: list[str] = []
        for form_field in form.fields:
            if form_field.field_type == "file":
                continue
            answer = answers.get(form_field.name)
            if answer is None or not answer.answer:
                continue
            if answer.needs_user:
                # Should be impossible after the checklist, but a second look here
                # costs nothing and a wrong answer on a form cannot be undone.
                return self._stop(
                    application_id,
                    safety.SafetyVerdict(
                        safe=False,
                        reason=StopReason.UNKNOWN_HIGH_IMPACT_QUESTION,
                        detail=f"field {form_field.name!r} is still unconfirmed",
                    ),
                )
            self._driver.fill(form_field.name, answer.answer)
            filled.append(form_field.name)

        self._attach_resume(application, form)

        state = self._driver.submit()

        # An uncertain outcome is not a failure to retry: it is a state only a human
        # can resolve, because a blind retry risks a duplicate application.
        if state.status_code >= 500:
            return self._stop(
                application_id,
                safety.SafetyVerdict(
                    safe=False,
                    reason=StopReason.NETWORK_UNCERTAIN,
                    detail=(
                        f"the site returned HTTP {state.status_code} after submitting. "
                        "The application may or may not have been received — check the "
                        "site before trying again."
                    ),
                ),
                status=ApplicationStatus.VERIFICATION_REQUIRED,
            )

        verdict = state.verdict()
        if not verdict.safe:
            return self._stop(
                application_id, verdict, status=ApplicationStatus.VERIFICATION_REQUIRED
            )

        if not safety.looks_like_confirmation(state.html):
            return self._stop(
                application_id,
                safety.SafetyVerdict(
                    safe=False,
                    reason=StopReason.UNEXPECTED_STRUCTURE,
                    detail=(
                        "no confirmation was found after submitting, so the outcome is "
                        "unverified. Check the site rather than resubmitting."
                    ),
                ),
                status=ApplicationStatus.VERIFICATION_REQUIRED,
            )

        reference = safety.extract_confirmation_ref(state.html)
        submitted = self._applications.record_submitted(application_id, confirmation_ref=reference)
        self._store_snapshot(application_id, state, label="confirmation")

        logger.info(
            "runner.submitted",
            extra={
                "application_id": application_id,
                "confirmation_ref": reference,
                "idempotency_key": idempotency_key,
                "fields_filled": len(filled),
            },
        )
        return RunResult(
            application=submitted,
            form=form,
            filled_fields=filled,
            confirmation_ref=reference,
        )

    def _attach_resume(self, application: Application, form: FormSpec) -> None:
        """Attach the exact approved resume artifact (FR-44)."""
        file_fields = [item for item in form.fields if item.field_type == "file"]
        if not file_fields:
            return
        if application.approved_resume_id is None:
            raise SafetyStop(
                "the form requires a file upload but no approved resume is attached",
                reason=StopReason.UNEXPECTED_STRUCTURE.value,
            )

        resume = self._session.get(ResumeRow, application.approved_resume_id)
        if resume is None:
            raise NotFoundError(f"resume {application.approved_resume_id} not found")

        pdf = self._resolve_pdf(resume)
        for item in file_fields:
            self._driver.attach_file(item.name, pdf)

    def _resolve_pdf(self, resume: ResumeRow) -> Path:
        """Find the compiled PDF for a resume version.

        The .tex is the source of truth, but an employer needs the PDF. Refusing when
        it is missing is correct: uploading the wrong file is unrecoverable.
        """
        source = Path(resume.source_path)
        candidate = source.with_suffix(".pdf")
        if candidate.is_file():
            return candidate

        artifact = (
            self._session.query(ArtifactRow)
            .filter(
                ArtifactRow.type == ArtifactType.RESUME_PDF, ArtifactRow.sha256 == resume.sha256
            )
            .first()
        )
        if artifact is not None and Path(artifact.path).is_file():
            return Path(artifact.path)

        raise SafetyStop(
            "no compiled PDF exists for the approved resume version; compile it before applying",
            reason=StopReason.UNEXPECTED_STRUCTURE.value,
            details={"resume_id": resume.id},
        )

    def _store_snapshot(self, application_id: str, state: PageState, *, label: str) -> None:
        directory = self._snapshot_dir(application_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{label}.html"
        path.write_text(safety.redact_html(state.html, limit=200_000), encoding="utf-8")

        self._session.add(
            ArtifactRow(
                id=new_id("artifact"),
                type=ArtifactType.PAGE_SNAPSHOT,
                path=str(path),
                sha256=sha256_file(path),
                application_id=application_id,
            )
        )
        self._session.flush()
