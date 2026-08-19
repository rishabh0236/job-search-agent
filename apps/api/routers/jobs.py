"""Job discovery and match endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field

from apps.api.deps import LLMDep, SessionDep, SettingsDep
from packages.schemas.job import Job, JobSearchCriteria, SourceHealth
from packages.schemas.matching import JobMatch, ScoreWeights
from services.candidate.service import CandidateService
from services.jobs.service import JobService
from services.matching.service import MatchService

router = APIRouter(tags=["jobs"])


class DiscoverRequest(BaseModel):
    criteria: JobSearchCriteria = Field(default_factory=JobSearchCriteria)
    #: Restrict the run to named sources; omit to use every configured one.
    sources: list[str] | None = None


class DiscoverResponse(BaseModel):
    stored: int
    unique_postings: int
    jobs: list[Job]


class MatchRequest(BaseModel):
    candidate_id: str
    #: Score these jobs; omit to score the stored feed.
    job_ids: list[str] | None = None
    weights: ScoreWeights | None = None
    explain: bool = False
    limit: int = Field(default=50, ge=1, le=200)


class MatchListResponse(BaseModel):
    matches: list[JobMatch]
    jobs: dict[str, Job] = Field(
        default_factory=dict, description="Jobs referenced by the matches, keyed by id"
    )


@router.get("/jobs/sources", response_model=list[SourceHealth])
def source_health(session: SessionDep, settings: SettingsDep) -> list[SourceHealth]:
    """Per-source health, so a broken adapter is visible before a discovery run."""
    return JobService(session, settings=settings).source_health()


@router.post("/jobs/discover", response_model=DiscoverResponse, status_code=status.HTTP_201_CREATED)
def discover(
    payload: DiscoverRequest, session: SessionDep, settings: SettingsDep
) -> DiscoverResponse:
    service = JobService(session, settings=settings)
    jobs = service.discover(payload.criteria, source_names=payload.sources)
    return DiscoverResponse(
        stored=len(jobs),
        unique_postings=len({job.dedupe_group or job.id for job in jobs}),
        jobs=jobs,
    )


@router.get("/jobs", response_model=list[Job])
def list_jobs(
    session: SessionDep,
    settings: SettingsDep,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    company: str | None = None,
    collapse_duplicates: bool = True,
) -> list[Job]:
    return JobService(session, settings=settings).list_jobs(
        limit=limit, offset=offset, company=company, collapse_duplicates=collapse_duplicates
    )


@router.get("/jobs/{job_id}", response_model=Job)
def get_job(job_id: str, session: SessionDep, settings: SettingsDep) -> Job:
    return JobService(session, settings=settings).get(job_id)


@router.get("/jobs/{job_id}/duplicates", response_model=list[Job])
def job_duplicates(job_id: str, session: SessionDep, settings: SettingsDep) -> list[Job]:
    """Other stored postings judged to be the same role, with the reason logged."""
    return JobService(session, settings=settings).duplicates_of(job_id)


@router.post("/matches", response_model=MatchListResponse, status_code=status.HTTP_201_CREATED)
def compute_matches(
    payload: MatchRequest,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> MatchListResponse:
    """Score a candidate against jobs.

    Explanations are opt-in: scoring a feed should not cost one model call per job.
    """
    jobs_service = JobService(session, settings=settings)
    profile = CandidateService(session, llm, settings).get_profile(payload.candidate_id)

    if payload.job_ids:
        jobs = [jobs_service.get(job_id) for job_id in payload.job_ids]
    else:
        jobs = jobs_service.list_jobs(limit=payload.limit)

    matches = MatchService(session, llm).match_many(
        profile, jobs, weights=payload.weights, explain=payload.explain
    )
    return MatchListResponse(
        matches=matches, jobs={job.id: job for job in jobs if job.id in {m.job_id for m in matches}}
    )


@router.get("/candidates/{candidate_id}/matches", response_model=MatchListResponse)
def list_matches(
    candidate_id: str,
    session: SessionDep,
    settings: SettingsDep,
    limit: int = Query(default=50, ge=1, le=200),
    min_score: float = Query(default=0.0, ge=0.0, le=1.0),
    include_ineligible: bool = True,
) -> MatchListResponse:
    matches = MatchService(session).list_for_candidate(
        candidate_id,
        limit=limit,
        min_score=min_score,
        include_ineligible=include_ineligible,
    )
    jobs_service = JobService(session, settings=settings)
    return MatchListResponse(
        matches=matches,
        jobs={match.job_id: jobs_service.get(match.job_id) for match in matches},
    )


@router.get("/candidates/{candidate_id}/matches/{job_id}", response_model=JobMatch)
def get_match(candidate_id: str, job_id: str, session: SessionDep) -> JobMatch:
    return MatchService(session).get(candidate_id, job_id)


@router.post("/candidates/{candidate_id}/matches/{job_id}/explain", response_model=JobMatch)
def explain_match(
    candidate_id: str,
    job_id: str,
    session: SessionDep,
    llm: LLMDep,
    settings: SettingsDep,
) -> JobMatch:
    """Recompute one match with a written explanation (the job-detail screen)."""
    profile = CandidateService(session, llm, settings).get_profile(candidate_id)
    job = JobService(session, settings=settings).get(job_id)
    return MatchService(session, llm).match(profile, job, explain=True)
