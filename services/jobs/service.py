"""Job discovery orchestration.

Responsibilities: run the configured sources, normalize, deduplicate against both
the current batch and what is already stored, persist with provenance, and audit.

Nothing here trusts a source's output beyond its schema. A description is stored as
data and only ever reaches a model fenced as untrusted content.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from packages.core import audit
from packages.core.db.models import Job as JobRow
from packages.core.errors import NotFoundError, ValidationFailed
from packages.core.logging import get_logger
from packages.core.settings import Settings, get_settings
from packages.schemas.enums import EmploymentType, RemoteMode
from packages.schemas.job import (
    Job,
    JobRequirement,
    JobSearchCriteria,
    SalaryRange,
    SourceHealth,
)
from services.jobs import dedupe, requirements
from services.jobs.base import JobSource, SourceRegistry
from services.jobs.sources import (
    AdzunaSource,
    ArbeitnowSource,
    AshbySource,
    CareerPageSource,
    GreenhouseSource,
    LeverSource,
    LocalFixtureSource,
    SmartRecruitersSource,
)

logger = get_logger(__name__)


def build_registry(settings: Settings | None = None) -> SourceRegistry:
    """Construct the configured sources.

    The local fixture source is always present: discovery, matching and tailoring
    must be developable and testable without touching a third-party service.
    """
    settings = settings or get_settings()
    registry = SourceRegistry()
    registry.register(LocalFixtureSource(settings.data_dir / "jobs" / "fixtures.json"))

    if settings.greenhouse_boards:
        registry.register(GreenhouseSource(settings.greenhouse_boards))

    if settings.lever_sites:
        registry.register(LeverSource(settings.lever_sites))

    if settings.ashby_boards:
        registry.register(AshbySource(settings.ashby_boards))

    if settings.smartrecruiters_companies:
        registry.register(SmartRecruitersSource(settings.smartrecruiters_companies))

    if (
        settings.adzuna_app_id
        and settings.adzuna_app_key
        and settings.adzuna_app_key.get_secret_value()
    ):
        registry.register(
            AdzunaSource(
                app_id=settings.adzuna_app_id,
                app_key=settings.adzuna_app_key.get_secret_value(),
                country=settings.adzuna_country,
            )
        )

    if settings.arbeitnow_enabled:
        registry.register(ArbeitnowSource())

    if settings.career_pages:
        registry.register(CareerPageSource(settings.career_pages))

    return registry


def row_to_job(row: JobRow) -> Job:
    """Rehydrate the domain object from its stored form."""
    requirement_payload = row.requirements_json or {}
    items = requirement_payload.get("items", []) if isinstance(requirement_payload, dict) else []

    return Job(
        id=row.id,
        source=row.source,
        source_job_id=row.source_job_id,
        company=row.company,
        title=row.title,
        location=row.location,
        remote=RemoteMode(row.remote),
        employment_type=EmploymentType(row.employment_type),
        description=row.description,
        requirements=[JobRequirement.model_validate(item) for item in items],
        salary=SalaryRange.model_validate(row.salary_json) if row.salary_json else None,
        url=row.url,
        posted_at=row.posted_at,
        retrieved_at=row.retrieved_at,
        dedupe_group=row.dedupe_group,
        raw=row.raw_json or {},
    )


class JobService:
    def __init__(
        self,
        session: Session,
        registry: SourceRegistry | None = None,
        settings: Settings | None = None,
    ) -> None:
        self._session = session
        self._settings = settings or get_settings()
        self._registry = registry or build_registry(self._settings)

    # ------------------------------------------------------------------ sources

    def source_health(self) -> list[SourceHealth]:
        return [source.health_check() for source in self._registry.all()]

    def source_names(self) -> list[str]:
        return self._registry.names()

    # ---------------------------------------------------------------- discovery

    def discover(
        self,
        criteria: JobSearchCriteria,
        *,
        source_names: list[str] | None = None,
    ) -> list[Job]:
        """Search sources, normalize, dedupe and persist. Returns stored jobs."""
        sources: list[JobSource] = []
        if source_names:
            for name in source_names:
                source = self._registry.get(name)
                if source is None:
                    raise ValidationFailed(
                        f"unknown job source {name!r}",
                        details={"available": self._registry.names()},
                    )
                sources.append(source)
        else:
            sources = self._registry.all()

        found: list[Job] = []
        for source in sources:
            try:
                found.extend(source.search(criteria))
            except Exception as exc:
                logger.warning(
                    "jobs.search_failed",
                    extra={"source": source.name, "error": type(exc).__name__},
                )

        # Requirement extraction is deterministic and cheap; do it before storing so
        # the job is immediately scoreable.
        suspicious_total = 0
        for job in found:
            if not job.requirements:
                job.requirements = requirements.extract_requirements(job.description)

            suspicious = requirements.detect_suspicious_instructions(job.description)
            if suspicious:
                # Recorded, not hidden: a posting trying to manipulate automated
                # screening is something the candidate should be able to see.
                suspicious_total += len(suspicious)
                job.raw = {**job.raw, "suspicious_instructions": suspicious}
                logger.warning(
                    "jobs.suspicious_instructions",
                    extra={
                        "source": job.source,
                        "source_job_id": job.source_job_id,
                        "count": len(suspicious),
                    },
                )

        groups = dedupe.assign_groups(found)
        stored: list[Job] = []
        created = updated = 0

        for job in found:
            job.dedupe_group = groups[job.id]
            row = self._session.scalar(
                select(JobRow).where(
                    JobRow.source == job.source, JobRow.source_job_id == job.source_job_id
                )
            )

            if row is None:
                # Match against already-stored jobs from other sources so a
                # cross-posted role joins its existing group rather than starting one.
                existing_group = self._existing_group_for(job)
                if existing_group is not None:
                    job.dedupe_group = existing_group

                row = JobRow(
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
                    salary_json=job.salary.model_dump(mode="json") if job.salary else {},
                    url=str(job.url) if job.url else None,
                    posted_at=job.posted_at,
                    retrieved_at=job.retrieved_at,
                    dedupe_group=job.dedupe_group,
                    raw_json=job.raw,
                )
                self._session.add(row)
                created += 1
            else:
                # Refresh mutable fields; identity and id stay put so matches and
                # applications keep pointing at the same job.
                row.title = job.title
                row.location = job.location
                row.description = job.description
                row.requirements_json = {
                    "items": [item.model_dump(mode="json") for item in job.requirements]
                }
                row.retrieved_at = job.retrieved_at
                if job.salary:
                    row.salary_json = job.salary.model_dump(mode="json")
                updated += 1

            self._session.flush()
            stored.append(row_to_job(row))

        audit.record(
            self._session,
            actor=audit.ACTOR_SYSTEM,
            action="jobs.discovered",
            entity_type="job_search",
            entity_id=",".join(source.name for source in sources) or "none",
            metadata={
                "found": len(found),
                "created": created,
                "updated": updated,
                "titles": criteria.titles,
                "groups": len({job.dedupe_group for job in stored if job.dedupe_group}),
                "suspicious_instruction_lines": suspicious_total,
            },
        )
        logger.info(
            "jobs.discovered",
            extra={
                "found": len(found),
                "jobs_created": created,
                "jobs_updated": updated,
            },
        )
        return stored

    def _existing_group_for(self, job: Job) -> str | None:
        """Find the dedupe group of an already-stored duplicate, if any."""
        key = dedupe.DedupeKey.of(job)
        candidates = self._session.scalars(
            select(JobRow).where(JobRow.company == job.company).limit(200)
        ).all()
        for row in candidates:
            duplicate, reason = dedupe.is_duplicate(key, dedupe.DedupeKey.of(row_to_job(row)))
            if duplicate:
                logger.info(
                    "jobs.duplicate_detected",
                    extra={"existing_job_id": row.id, "reason": reason},
                )
                return row.dedupe_group
        return None

    # ------------------------------------------------------------------ reading

    def get(self, job_id: str) -> Job:
        row = self._session.get(JobRow, job_id)
        if row is None:
            raise NotFoundError(f"job {job_id} not found")
        return row_to_job(row)

    def list_jobs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        company: str | None = None,
        collapse_duplicates: bool = True,
    ) -> list[Job]:
        """List stored jobs, newest retrieval first.

        Duplicates are collapsed to one row per dedupe group by default: the feed
        should show a role once, no matter how many boards carry it.
        """
        statement = select(JobRow).order_by(JobRow.retrieved_at.desc())
        if company:
            statement = statement.where(JobRow.company.ilike(f"%{company}%"))

        rows = self._session.scalars(statement).all()

        if collapse_duplicates:
            seen: set[str] = set()
            unique: list[JobRow] = []
            for row in rows:
                group = row.dedupe_group or row.id
                if group in seen:
                    continue
                seen.add(group)
                unique.append(row)
            rows = unique

        return [row_to_job(row) for row in rows[offset : offset + limit]]

    def count(self) -> int:
        return self._session.scalar(select(func.count()).select_from(JobRow)) or 0

    def duplicates_of(self, job_id: str) -> list[Job]:
        """Other stored postings judged to be the same role."""
        job = self.get(job_id)
        if not job.dedupe_group:
            return []
        rows = self._session.scalars(
            select(JobRow).where(JobRow.dedupe_group == job.dedupe_group, JobRow.id != job_id)
        ).all()
        return [row_to_job(row) for row in rows]


def write_fixture_jobs(settings: Settings, payload: str) -> Path:
    """Write the local fixture file, creating its directory. Used by seeding."""
    path = settings.data_dir / "jobs" / "fixtures.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")
    return path
