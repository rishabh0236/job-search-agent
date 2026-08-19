"""Match orchestration.

Deterministic code produces the score, the eligibility verdict, the strengths and
the gaps. The model is asked for one thing only: prose that explains that result,
citing evidence ids it was given. If the explanation fails or cites something it was
not handed, the match is still valid — it just ships without prose.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from packages.core import audit
from packages.core.db.models import JobMatch as JobMatchRow
from packages.core.errors import DomainError, NotFoundError
from packages.core.ids import new_id
from packages.core.llm.base import LLMRequest, UntrustedContent
from packages.core.llm.client import LLMClient
from packages.core.logging import get_logger
from packages.prompts.matching import (
    MATCH_EXPLAINER_SYSTEM,
    MATCH_EXPLAINER_VERSION,
    match_explainer_user_message,
)
from packages.schemas.candidate import CandidateProfile
from packages.schemas.enums import Eligibility
from packages.schemas.job import Job
from packages.schemas.llm_tasks import TASK_MATCH_EXPLAINER, MatchExplanationOutput
from packages.schemas.matching import JobMatch, MatchedRequirement, ScoreWeights
from services.matching import scoring

logger = get_logger(__name__)


def _json_items(payload: dict[str, Any] | None) -> list[Any]:
    """Read an ``{"items": [...]}`` JSON column safely.

    The column is typed ``dict[str, Any]``, so its members are ``Any`` as far as the
    type checker is concerned; this keeps the narrowing in one place instead of at
    every read site.
    """
    items = (payload or {}).get("items", [])
    return items if isinstance(items, list) else []


class MatchService:
    def __init__(self, session: Session, llm: LLMClient | None = None) -> None:
        self._session = session
        self._llm = llm

    def match(
        self,
        profile: CandidateProfile,
        job: Job,
        *,
        weights: ScoreWeights | None = None,
        explain: bool = True,
    ) -> JobMatch:
        """Score one candidate against one job and persist the result."""
        job = scoring.infer_requirement_keys(job)
        payload = scoring.ScoringInput(
            job=job,
            facts=profile.facts,
            preferences=profile.preferences,
            weights=weights or ScoreWeights(),
        )

        hard = scoring.evaluate_hard_constraints(payload)
        components, strengths, gaps = scoring.score_components(payload, hard)
        score = scoring.total_score(components)
        uncertainty = scoring.derive_uncertainty(hard, gaps, payload)

        match = JobMatch(
            id=new_id("job_match"),
            job_id=job.id,
            candidate_id=profile.id,
            score=score,
            eligibility=hard.eligibility,
            hard_constraints=hard,
            components=components,
            strengths=strengths,
            gaps=gaps,
            uncertainty=uncertainty,
            weights_used=payload.weights.as_dict(),
        )

        if explain and self._llm is not None:
            match.explanation = self._explain(profile, job, match)

        return self._persist(match)

    def match_many(
        self,
        profile: CandidateProfile,
        jobs: list[Job],
        *,
        weights: ScoreWeights | None = None,
        explain: bool = False,
    ) -> list[JobMatch]:
        """Score a batch, best first.

        Explanations are off by default here: scoring a feed of fifty jobs should not
        cost fifty model calls. The detail screen explains one job on demand.
        """
        matches = [self.match(profile, job, weights=weights, explain=explain) for job in jobs]
        matches.sort(key=lambda item: (-item.score, item.job_id))
        return matches

    # ------------------------------------------------------------- explanation

    def _explain(self, profile: CandidateProfile, job: Job, match: JobMatch) -> str:
        if self._llm is None:  # pragma: no cover - guarded by the caller
            return ""

        allowed = frozenset(ref.evidence_id for fact in profile.facts for ref in fact.evidence)
        evidence_lines = [
            f"[{ref.evidence_id}] {ref.quote}"
            for fact in profile.facts
            for ref in fact.evidence[:1]
        ]

        request: LLMRequest[MatchExplanationOutput] = LLMRequest(
            task=TASK_MATCH_EXPLAINER,
            system=MATCH_EXPLAINER_SYSTEM,
            blocks=[
                match_explainer_user_message(
                    score=match.score,
                    eligibility=match.eligibility.value,
                    strengths=[item.requirement for item in match.strengths],
                    gaps=[item.requirement for item in match.gaps],
                    unknowns=match.uncertainty,
                    evidence_listing="\n".join(evidence_lines[:60]),
                ),
                # The posting is third-party text: fenced, never trusted.
                UntrustedContent(
                    label=f"{job.source}:{job.source_job_id}",
                    text=f"{job.title} at {job.company}\n\n{job.description[:6000]}",
                ),
            ],
            output_model=MatchExplanationOutput,
            temperature=0.0,
            max_tokens=2000,
            allowed_evidence_ids=allowed,
        )

        try:
            result = self._llm.run(request)
        except DomainError as exc:
            # A missing explanation degrades the UI; a wrong score would mislead. The
            # score is already computed, so this failure is genuinely non-fatal.
            logger.warning(
                "matching.explanation_failed",
                extra={"job_id": job.id, "code": exc.code, "detail": exc.message},
            )
            return ""
        return result.output.explanation

    # ------------------------------------------------------------- persistence

    def _persist(self, match: JobMatch) -> JobMatch:
        row = self._session.scalar(
            select(JobMatchRow).where(
                JobMatchRow.candidate_id == match.candidate_id,
                JobMatchRow.job_id == match.job_id,
            )
        )

        def _dump(items: list[MatchedRequirement]) -> dict[str, object]:
            return {"items": [item.model_dump(mode="json") for item in items]}

        if row is None:
            row = JobMatchRow(id=match.id, job_id=match.job_id, candidate_id=match.candidate_id)
            self._session.add(row)
        else:
            match = match.model_copy(update={"id": row.id})

        row.score = match.score
        row.eligibility = match.eligibility
        row.hard_constraints_json = match.hard_constraints.model_dump(mode="json")
        row.components_json = {
            "items": [component.model_dump(mode="json") for component in match.components]
        }
        row.strengths_json = _dump(match.strengths)
        row.gaps_json = _dump(match.gaps)
        row.explanation = match.explanation
        row.weights_json = match.weights_used
        self._session.flush()

        audit.record(
            self._session,
            actor=audit.ACTOR_SYSTEM,
            action="match.scored",
            entity_type="job_match",
            entity_id=row.id,
            metadata={
                "job_id": match.job_id,
                "score": match.score,
                "eligibility": match.eligibility.value,
                "components": {c.name: round(c.weighted, 4) for c in match.components},
                "explained": bool(match.explanation),
                "prompt_version": MATCH_EXPLAINER_VERSION,
            },
        )
        return match

    # ----------------------------------------------------------------- reading

    def get(self, candidate_id: str, job_id: str) -> JobMatch:
        row = self._session.scalar(
            select(JobMatchRow).where(
                JobMatchRow.candidate_id == candidate_id, JobMatchRow.job_id == job_id
            )
        )
        if row is None:
            raise NotFoundError(f"no match stored for job {job_id}")
        return self._row_to_match(row)

    def list_for_candidate(
        self,
        candidate_id: str,
        *,
        limit: int = 50,
        min_score: float = 0.0,
        include_ineligible: bool = True,
    ) -> list[JobMatch]:
        statement = (
            select(JobMatchRow)
            .where(JobMatchRow.candidate_id == candidate_id, JobMatchRow.score >= min_score)
            .order_by(JobMatchRow.score.desc())
        )
        rows = self._session.scalars(statement).all()
        matches = [self._row_to_match(row) for row in rows]
        if not include_ineligible:
            matches = [m for m in matches if m.eligibility is not Eligibility.INELIGIBLE]
        return matches[:limit]

    def _row_to_match(self, row: JobMatchRow) -> JobMatch:
        from packages.schemas.matching import HardConstraintResult, ScoreComponent

        def _load(payload: dict[str, Any] | None) -> list[MatchedRequirement]:
            return [MatchedRequirement.model_validate(item) for item in _json_items(payload)]

        components = [
            ScoreComponent.model_validate(item) for item in _json_items(row.components_json)
        ]

        return JobMatch(
            id=row.id,
            job_id=row.job_id,
            candidate_id=row.candidate_id,
            score=row.score,
            eligibility=row.eligibility,
            hard_constraints=HardConstraintResult.model_validate(row.hard_constraints_json or {}),
            components=components,
            strengths=_load(row.strengths_json),
            gaps=_load(row.gaps_json),
            explanation=row.explanation,
            weights_used=dict(row.weights_json or {}),
        )
