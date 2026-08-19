"""Candidate, profile and resume-ingestion endpoints.

Thin by design: parse, delegate to :class:`CandidateService`, serialise. All
validation and every guard lives in the service layer, so the same behaviour is
available to scripts and tests without going through HTTP.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from fastapi import APIRouter, File, Form, UploadFile, status
from pydantic import BaseModel, Field

from apps.api.deps import LLMDep, SessionDep, SettingsDep
from packages.core.db.models import CandidateFact as CandidateFactRow
from packages.core.errors import ValidationFailed
from packages.schemas.candidate import CandidateFact, CandidatePreferences, CandidateProfile
from packages.schemas.enums import FactCategory, SourceType
from packages.schemas.ingestion import IngestionReport
from services.candidate.evidence import to_ref
from services.candidate.service import CandidateService

router = APIRouter(prefix="/candidates", tags=["candidates"])

#: Upload ceiling. A resume is a handful of pages; anything larger is a mistake or
#: an attempt to exhaust local disk.
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

ALLOWED_SUFFIXES = {".pdf", ".tex", ".latex", ".txt", ".md"}


class CreateCandidateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    preferences: CandidatePreferences | None = None


class CandidateSummary(BaseModel):
    id: str
    display_name: str | None
    fact_count: int
    unresolved_count: int


class CorrectFactRequest(BaseModel):
    claim: str = Field(min_length=1, max_length=1000)


class AddFactRequest(BaseModel):
    category: FactCategory
    claim: str = Field(min_length=1, max_length=1000)
    attributes: dict[str, str] = Field(default_factory=dict)


def _service(session: SessionDep, llm: LLMDep, settings: SettingsDep) -> CandidateService:
    return CandidateService(session, llm, settings)


def _to_fact_schema(row: CandidateFactRow) -> CandidateFact:
    """Serialise an ORM fact, including its resolved evidence."""
    return CandidateFact(
        id=row.id,
        candidate_id=row.candidate_id,
        category=row.category,
        claim=row.claim,
        attributes=dict(row.attributes),
        evidence=[to_ref(record) for record in row.evidence],
        confidence=row.confidence,
        provenance=row.provenance,
        verified=row.verified,
    )


@router.post("", response_model=CandidateSummary, status_code=status.HTTP_201_CREATED)
def create_candidate(
    payload: CreateCandidateRequest,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> CandidateSummary:
    service = _service(session, llm, settings)
    candidate = service.create_candidate(
        display_name=payload.display_name, preferences=payload.preferences
    )
    return CandidateSummary(
        id=candidate.id,
        display_name=candidate.display_name,
        fact_count=0,
        unresolved_count=0,
    )


@router.get("/{candidate_id}", response_model=CandidateProfile)
def get_profile(
    candidate_id: str,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> CandidateProfile:
    return _service(session, llm, settings).get_profile(candidate_id)


@router.put("/{candidate_id}/preferences", response_model=CandidateProfile)
def update_preferences(
    candidate_id: str,
    preferences: CandidatePreferences,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> CandidateProfile:
    service = _service(session, llm, settings)
    service.update_preferences(candidate_id, preferences)
    return service.get_profile(candidate_id)


@router.post(
    "/{candidate_id}/resumes",
    response_model=IngestionReport,
    status_code=status.HTTP_201_CREATED,
    summary="Ingest a resume file into evidence and canonical facts",
)
async def ingest_resume(
    candidate_id: str,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
    file: UploadFile = File(description="Resume as .pdf, .tex, .txt or .md"),
    is_original: bool | None = Form(default=None),
) -> IngestionReport:
    """Upload and ingest a resume.

    The file is streamed to a temporary path, size-checked, then handed to the
    service, which copies it into the immutable content-addressed store. The temp
    copy is always removed, including on failure.
    """
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValidationFailed(
            f"unsupported file type {suffix or '(none)'}",
            details={"allowed": sorted(ALLOWED_SUFFIXES)},
        )

    temp_dir = Path(tempfile.mkdtemp(prefix="career-agent-upload-"))
    temp_path = temp_dir / (Path(file.filename or "resume").name)
    try:
        written = 0
        with temp_path.open("wb") as handle:
            while chunk := await file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    raise ValidationFailed(
                        "resume exceeds the 10 MB upload limit",
                        details={"limit_bytes": MAX_UPLOAD_BYTES},
                    )
                handle.write(chunk)

        if written == 0:
            raise ValidationFailed("uploaded file is empty")

        service = _service(session, llm, settings)
        return service.ingest_resume(
            candidate_id,
            temp_path,
            source_type=SourceType.LATEX if suffix in (".tex", ".latex") else None,
            is_original=is_original,
        )
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


@router.post("/{candidate_id}/facts", response_model=CandidateFact, status_code=201)
def add_fact(
    candidate_id: str,
    payload: AddFactRequest,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> CandidateFact:
    service = _service(session, llm, settings)
    fact = service.add_fact(
        candidate_id,
        category=payload.category,
        claim=payload.claim,
        attributes=payload.attributes,
    )
    return _to_fact_schema(fact)


@router.post("/{candidate_id}/facts/{fact_id}/verify", response_model=CandidateFact)
def verify_fact(
    candidate_id: str,
    fact_id: str,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> CandidateFact:
    service = _service(session, llm, settings)
    return _to_fact_schema(service.verify_fact(candidate_id, fact_id))


@router.patch("/{candidate_id}/facts/{fact_id}", response_model=CandidateFact)
def correct_fact(
    candidate_id: str,
    fact_id: str,
    payload: CorrectFactRequest,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> CandidateFact:
    service = _service(session, llm, settings)
    return _to_fact_schema(service.correct_fact(candidate_id, fact_id, payload.claim))


@router.delete("/{candidate_id}/facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_fact(
    candidate_id: str,
    fact_id: str,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> None:
    _service(session, llm, settings).delete_fact(candidate_id, fact_id)
