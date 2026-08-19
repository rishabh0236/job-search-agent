"""Resume tailoring and application endpoints."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from apps.api.deps import LLMDep, SessionDep, SettingsDep
from packages.core.errors import NotFoundError
from packages.schemas.application import Application, FormSpec
from packages.schemas.enums import TailoringMode
from packages.schemas.resume import TailoringResult
from services.application.service import ApplicationService
from services.jobs.service import JobService
from services.resume.tailoring import TailoringService

router = APIRouter(tags=["applications"])


class TailorRequest(BaseModel):
    candidate_id: str
    resume_id: str
    job_id: str
    mode: TailoringMode = TailoringMode.BALANCED
    max_edits: int = Field(default=20, ge=1, le=100)


class DiffEntry(BaseModel):
    target_id: str
    before: str
    after: str
    rationale: str
    status: str


class CreateApplicationRequest(BaseModel):
    candidate_id: str
    job_id: str


class PrepareRequest(BaseModel):
    form: FormSpec
    approved_resume_id: str | None = None
    write_cover_letter: bool = True


class AnswerRequest(BaseModel):
    field: str
    answer: str


class ChecklistResponse(BaseModel):
    ready: bool
    blockers: list[str]


# ------------------------------------------------------------------- tailoring


@router.post("/resumes/tailor", response_model=TailoringResult, status_code=status.HTTP_201_CREATED)
def tailor(
    payload: TailorRequest, session: SessionDep, llm: LLMDep, settings: SettingsDep
) -> TailoringResult:
    """Tailor a resume for a job.

    Returns the result even when it is blocked: the review screen needs to show what
    was rejected and why, not just a failure.
    """
    job = JobService(session, settings=settings).get(payload.job_id)
    return TailoringService(session, llm, settings).tailor(
        payload.candidate_id,
        payload.resume_id,
        job,
        mode=payload.mode,
        max_edits=payload.max_edits,
    )


@router.get("/resumes/{resume_id}/diff", response_model=list[DiffEntry])
def resume_diff(
    resume_id: str, session: SessionDep, llm: LLMDep, settings: SettingsDep
) -> list[DiffEntry]:
    entries = TailoringService(session, llm, settings).diff(resume_id)
    return [DiffEntry.model_validate(entry) for entry in entries]


@router.get("/resumes/{resume_id}/source", response_class=FileResponse)
def resume_source(
    resume_id: str, session: SessionDep, llm: LLMDep, settings: SettingsDep
) -> FileResponse:
    """Download the .tex source of a version."""
    service = TailoringService(session, llm, settings)
    service.load_source(resume_id)  # validates existence and type
    from packages.core.db.models import Resume as ResumeRow

    row = session.get(ResumeRow, resume_id)
    if row is None:  # pragma: no cover - load_source already raised
        raise NotFoundError(f"resume {resume_id} not found")
    return FileResponse(
        row.source_path, media_type="text/x-tex", filename=Path(row.source_path).name
    )


# ----------------------------------------------------------------- applications


@router.post("/applications", response_model=Application, status_code=status.HTTP_201_CREATED)
def create_application(
    payload: CreateApplicationRequest, session: SessionDep, llm: LLMDep, settings: SettingsDep
) -> Application:
    return ApplicationService(session, llm, settings).create(payload.candidate_id, payload.job_id)


@router.get("/applications/{application_id}", response_model=Application)
def get_application(
    application_id: str, session: SessionDep, llm: LLMDep, settings: SettingsDep
) -> Application:
    return ApplicationService(session, llm, settings).get(application_id)


@router.get("/candidates/{candidate_id}/applications", response_model=list[Application])
def list_applications(
    candidate_id: str, session: SessionDep, llm: LLMDep, settings: SettingsDep
) -> list[Application]:
    """The application tracker."""
    return ApplicationService(session, llm, settings).list_for_candidate(candidate_id)


@router.post("/applications/{application_id}/prepare", response_model=Application)
def prepare_application(
    application_id: str,
    payload: PrepareRequest,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> Application:
    """Map answers and draft a cover letter, then move to review."""
    return ApplicationService(session, llm, settings).prepare(
        application_id,
        payload.form,
        approved_resume_id=payload.approved_resume_id,
        write_cover_letter=payload.write_cover_letter,
    )


@router.put("/applications/{application_id}/answers", response_model=Application)
def set_answer(
    application_id: str,
    payload: AnswerRequest,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> Application:
    """Provide or correct one answer. This is how sensitive fields get filled."""
    return ApplicationService(session, llm, settings).set_answer(
        application_id, payload.field, payload.answer
    )


@router.put("/applications/{application_id}/resume/{resume_id}", response_model=Application)
def attach_resume(
    application_id: str,
    resume_id: str,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> Application:
    return ApplicationService(session, llm, settings).attach_resume(application_id, resume_id)


@router.get("/applications/{application_id}/checklist", response_model=ChecklistResponse)
def checklist(
    application_id: str, session: SessionDep, llm: LLMDep, settings: SettingsDep
) -> ChecklistResponse:
    """Pre-submit checklist. The UI must not offer Submit while blockers remain."""
    blockers = ApplicationService(session, llm, settings).checklist(application_id)
    return ChecklistResponse(ready=not blockers, blockers=blockers)


@router.post("/applications/{application_id}/approve", response_model=Application)
def approve(
    application_id: str, session: SessionDep, llm: LLMDep, settings: SettingsDep
) -> Application:
    """Explicit human approval. Nothing may be submitted without it."""
    return ApplicationService(session, llm, settings).approve(application_id)
