"""Deterministic match scoring (PRD §10, FR-21 to FR-24).

Two rules shape everything here:

* **A missing fact is never a negative.** If the candidate's profile does not say
  whether they hold a work permit, that is an *unknown* to ask about, not a failed
  requirement. Unknowns are reported separately and excluded from the denominator,
  so an incomplete profile lowers confidence rather than manufacturing a low score.
* **Every number is reconstructable.** Each component records its raw score, weight
  and a human-readable rationale, so `JobMatch.recompute_score()` reproduces the
  total exactly and the UI can explain any figure it shows.

The LLM writes the prose explanation. It never touches the arithmetic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from packages.schemas.candidate import CandidateFact, CandidatePreferences
from packages.schemas.common import EvidenceRef
from packages.schemas.enums import (
    Eligibility,
    EmploymentType,
    FactCategory,
    Provenance,
    RemoteMode,
    RequirementKind,
)
from packages.schemas.job import Job, JobRequirement
from packages.schemas.matching import (
    HardConstraintResult,
    MatchedRequirement,
    ScoreComponent,
    ScoreWeights,
)
from services.jobs.requirements import normalized_key, required_years
from services.matching.retrieval import InMemoryIndex, text_similarity

#: A requirement is considered evidenced at or above this retrieval similarity.
#: Set high deliberately: a weak lexical overlap is not proof of experience, and an
#: over-generous threshold produces confident nonsense in the strengths list.
EVIDENCE_MATCH_THRESHOLD = 0.34

#: Below this, a requirement counts as a gap rather than an unknown.
GAP_THRESHOLD = 0.12


@dataclass(slots=True)
class ScoringInput:
    """Everything scoring needs, resolved up front by the service."""

    job: Job
    facts: list[CandidateFact]
    preferences: CandidatePreferences
    weights: ScoreWeights = field(default_factory=ScoreWeights)

    def usable_facts(self) -> list[CandidateFact]:
        """Facts that may inform a score.

        UNKNOWN-provenance facts are excluded: an unconfirmed observation must not
        become the reason a job ranks highly.
        """
        return [fact for fact in self.facts if fact.provenance is not Provenance.UNKNOWN]

    def skills(self) -> set[str]:
        return {
            fact.claim.strip().lower()
            for fact in self.usable_facts()
            if fact.category is FactCategory.SKILL
        }


def _evidence_refs(fact: CandidateFact, limit: int = 2) -> list[EvidenceRef]:
    return fact.evidence[:limit]


def build_evidence_index(facts: list[CandidateFact]) -> InMemoryIndex:
    index = InMemoryIndex()
    for fact in facts:
        index.add(fact.id, fact.claim)
    return index


# ------------------------------------------------------------------ hard filters


def _candidate_years(facts: list[CandidateFact]) -> int | None:
    """Total explicit years of experience, if the profile states any.

    Returns None rather than a guess. Deriving a number from date ranges is possible
    but would invent precision the resume did not claim, and years-of-experience is
    exactly the kind of figure that must never be fabricated.
    """
    values: list[int] = []
    for fact in facts:
        for key in ("years_experience", "years"):
            raw = fact.attributes.get(key)
            if isinstance(raw, (str, int)):
                match = re.search(r"\d{1,2}", str(raw))
                if match:
                    values.append(int(match.group()))
    return max(values) if values else None


def evaluate_hard_constraints(payload: ScoringInput) -> HardConstraintResult:
    """Eligibility gate. Blocking failures are facts, not inferences."""
    blocking: list[str] = []
    unknown: list[str] = []
    job, preferences = payload.job, payload.preferences

    # --- employment type
    if (
        preferences.employment_types
        and job.employment_type is not EmploymentType.UNKNOWN
        and job.employment_type not in preferences.employment_types
    ):
        blocking.append(
            f"employment type {job.employment_type.value} is outside the candidate's preferences"
        )

    # --- location / remote
    if preferences.remote_modes and job.remote is not RemoteMode.UNKNOWN:
        if job.remote not in preferences.remote_modes:
            if job.remote is RemoteMode.ONSITE and preferences.willing_to_relocate is not True:
                location_ok = any(
                    wanted.lower() in (job.location or "").lower()
                    for wanted in preferences.locations
                )
                if not location_ok:
                    blocking.append(
                        f"role is onsite in {job.location or 'an unlisted location'} and the "
                        "candidate has not indicated relocation"
                    )
            else:
                unknown.append(f"role is {job.remote.value}; candidate preference differs")
    elif job.remote is RemoteMode.UNKNOWN and preferences.remote_modes:
        unknown.append("posting does not state remote/onsite arrangement")

    # --- exclusions the candidate set explicitly
    haystack = f"{job.company} {job.title} {job.description}".lower()
    for exclusion in preferences.exclusions:
        if exclusion.strip() and exclusion.lower() in haystack:
            blocking.append(f"matches candidate exclusion {exclusion!r}")

    # --- years of experience
    demanded = required_years(job.requirements)
    if demanded is not None:
        held = _candidate_years(payload.usable_facts())
        if held is None:
            unknown.append(f"posting requires {demanded}+ years; profile does not state a total")
        elif held + 1 < demanded:
            # +1 tolerance: "5 years" postings routinely accept 4, and a hard reject
            # on a one-year gap would hide jobs the candidate should see.
            blocking.append(f"posting requires {demanded}+ years, profile states {held}")

    # --- work authorization: only ever from an explicit fact
    auth_required = re.search(
        r"\b(?:work authorization|right to work|visa sponsorship|security clearance)\b",
        job.description,
        re.IGNORECASE,
    )
    if auth_required is not None:
        stated = [
            fact
            for fact in payload.usable_facts()
            if fact.category is FactCategory.WORK_AUTHORIZATION
        ]
        if not stated:
            unknown.append("posting raises work authorization; candidate has not confirmed status")

    if blocking:
        eligibility = Eligibility.INELIGIBLE
    elif unknown:
        eligibility = Eligibility.UNKNOWN
    else:
        eligibility = Eligibility.ELIGIBLE

    return HardConstraintResult(eligibility=eligibility, blocking=blocking, unknown=unknown)


# --------------------------------------------------------------------- components


def _match_requirement(
    requirement: JobRequirement,
    payload: ScoringInput,
    index: InMemoryIndex,
    facts_by_id: dict[str, CandidateFact],
) -> MatchedRequirement:
    """Decide whether the profile evidences one requirement."""
    # Exact key match against a named skill is the strongest signal available.
    if requirement.key and requirement.key in payload.skills():
        fact = next(
            (
                item
                for item in payload.usable_facts()
                if item.category is FactCategory.SKILL
                and item.claim.strip().lower() == requirement.key
            ),
            None,
        )
        if fact is not None:
            return MatchedRequirement(
                requirement=requirement.text,
                satisfied=True,
                evidence=_evidence_refs(fact),
                note=f"named skill {fact.claim!r} in the profile",
            )

    results = index.query(requirement.text, top_k=3)
    if results and results[0].score >= EVIDENCE_MATCH_THRESHOLD:
        fact = facts_by_id[results[0].item_id]
        return MatchedRequirement(
            requirement=requirement.text,
            satisfied=True,
            evidence=_evidence_refs(fact),
            note=f"supported by {fact.category.value} evidence",
        )

    if results and results[0].score >= GAP_THRESHOLD:
        fact = facts_by_id[results[0].item_id]
        return MatchedRequirement(
            requirement=requirement.text,
            satisfied=None,
            evidence=_evidence_refs(fact, limit=1),
            note="partial overlap only; needs confirmation",
        )

    return MatchedRequirement(
        requirement=requirement.text,
        satisfied=False,
        note="no supporting evidence in the profile",
    )


def _ratio(satisfied: int, total: int) -> float:
    return satisfied / total if total else 0.0


def score_components(
    payload: ScoringInput,
    hard: HardConstraintResult,
) -> tuple[list[ScoreComponent], list[MatchedRequirement], list[MatchedRequirement]]:
    """Compute every weighted component plus the strengths and gaps lists."""
    weights = payload.weights
    facts = payload.usable_facts()
    facts_by_id = {fact.id: fact for fact in facts}
    index = build_evidence_index(facts)

    components: list[ScoreComponent] = []
    strengths: list[MatchedRequirement] = []
    gaps: list[MatchedRequirement] = []

    # --- 1. hard constraints
    hard_score = {
        Eligibility.ELIGIBLE: 1.0,
        Eligibility.UNKNOWN: 0.5,
        Eligibility.INELIGIBLE: 0.0,
    }[hard.eligibility]
    components.append(
        ScoreComponent(
            name="hard_constraints",
            raw_score=hard_score,
            weight=weights.hard_constraints,
            rationale=(
                f"eligibility {hard.eligibility.value}"
                + (f"; blocking: {'; '.join(hard.blocking)}" if hard.blocking else "")
                + (f"; unknown: {'; '.join(hard.unknown)}" if hard.unknown else "")
            ),
        )
    )

    # --- 2/6. requirement evidence, split by required vs preferred
    required = [r for r in payload.job.requirements if r.kind is RequirementKind.REQUIRED]
    preferred = [r for r in payload.job.requirements if r.kind is RequirementKind.PREFERRED]

    for bucket, name, weight in (
        (required, "required_skill_evidence", weights.required_skill_evidence),
        (preferred, "preferred_skills", weights.preferred_skills),
    ):
        matched = [_match_requirement(item, payload, index, facts_by_id) for item in bucket]
        satisfied = sum(1 for item in matched if item.satisfied is True)
        # Unknowns leave the denominator: an ambiguous requirement should not be
        # scored as if the candidate failed it.
        assessable = sum(1 for item in matched if item.satisfied is not None)

        if name == "required_skill_evidence":
            strengths.extend(item for item in matched if item.satisfied is True)
            gaps.extend(item for item in matched if item.satisfied is not True)
        else:
            strengths.extend(item for item in matched if item.satisfied is True)

        components.append(
            ScoreComponent(
                name=name,
                # No stated requirements of this kind is not a failure. Score it
                # neutral rather than zero, or every prose-only posting ranks last.
                raw_score=_ratio(satisfied, assessable) if assessable else 0.5,
                weight=weight,
                rationale=(
                    f"{satisfied}/{assessable} assessable requirements evidenced"
                    if assessable
                    else "posting states no requirements of this kind"
                ),
            )
        )

    # --- 3. semantic experience fit
    experience_text = " ".join(
        fact.claim
        for fact in facts
        if fact.category
        in (
            FactCategory.EXPERIENCE,
            FactCategory.ACHIEVEMENT,
            FactCategory.PROJECT,
            FactCategory.SUMMARY,
        )
    )
    job_text = f"{payload.job.title}\n{payload.job.description}"
    similarity = text_similarity(experience_text, job_text) if experience_text else 0.0
    components.append(
        ScoreComponent(
            name="semantic_experience_fit",
            # Lexical overlap between whole documents is naturally low; rescale so
            # the component uses its range instead of clustering near zero.
            raw_score=min(1.0, similarity * 2.5),
            weight=weights.semantic_experience_fit,
            rationale=f"experience/posting term overlap {similarity:.2f}",
        )
    )

    # --- 4. seniority
    components.append(_seniority_component(payload))

    # --- 5. location and preferences
    components.append(_location_component(payload))

    # --- 7. other preferences
    components.append(_other_preferences_component(payload))

    return components, strengths, gaps


_SENIORITY_LADDER = (
    ("intern", 0),
    ("junior", 1),
    ("associate", 1),
    ("mid", 2),
    ("senior", 3),
    ("staff", 4),
    ("principal", 5),
    ("lead", 4),
    ("head", 5),
    ("director", 6),
    ("vp", 7),
)


def _seniority_rank(text: str) -> int | None:
    lowered = text.lower()
    for token, rank in _SENIORITY_LADDER:
        if re.search(rf"\b{token}\b", lowered):
            return rank
    return None


def _seniority_component(payload: ScoringInput) -> ScoreComponent:
    job_rank = _seniority_rank(payload.job.title)
    target_ranks = [
        rank
        for role in payload.preferences.target_roles
        if (rank := _seniority_rank(f"{role.title} {role.seniority or ''}")) is not None
    ]

    if job_rank is None or not target_ranks:
        return ScoreComponent(
            name="seniority",
            raw_score=0.5,
            weight=payload.weights.seniority,
            rationale="seniority not stated on one side; scored neutral",
        )

    distance = min(abs(job_rank - rank) for rank in target_ranks)
    return ScoreComponent(
        name="seniority",
        raw_score=max(0.0, 1.0 - distance * 0.34),
        weight=payload.weights.seniority,
        rationale=f"seniority distance {distance} from the candidate's target level",
    )


def _location_component(payload: ScoringInput) -> ScoreComponent:
    job, preferences = payload.job, payload.preferences

    if job.remote is RemoteMode.REMOTE and RemoteMode.REMOTE in preferences.remote_modes:
        return ScoreComponent(
            name="location_preferences",
            raw_score=1.0,
            weight=payload.weights.location_preferences,
            rationale="remote role matches a remote preference",
        )

    if job.location and preferences.locations:
        location = job.location.lower()
        if any(wanted.lower() in location for wanted in preferences.locations):
            return ScoreComponent(
                name="location_preferences",
                raw_score=1.0,
                weight=payload.weights.location_preferences,
                rationale=f"location {job.location} is on the candidate's list",
            )
        return ScoreComponent(
            name="location_preferences",
            raw_score=0.2,
            weight=payload.weights.location_preferences,
            rationale=f"location {job.location} is not on the candidate's list",
        )

    return ScoreComponent(
        name="location_preferences",
        raw_score=0.5,
        weight=payload.weights.location_preferences,
        rationale="location not stated; scored neutral",
    )


def _other_preferences_component(payload: ScoringInput) -> ScoreComponent:
    job, preferences = payload.job, payload.preferences
    notes: list[str] = []
    score = 0.5

    if preferences.min_salary is not None and job.salary:
        upper = job.salary.max_amount or job.salary.min_amount
        if upper is not None:
            if upper >= preferences.min_salary:
                score = 1.0
                notes.append("advertised salary meets the candidate's minimum")
            else:
                score = 0.0
                notes.append("advertised salary is below the candidate's minimum")
    elif preferences.min_salary is not None:
        notes.append("posting does not advertise salary; scored neutral")

    if preferences.target_roles:
        keywords = {
            keyword.lower() for role in preferences.target_roles for keyword in role.keywords
        }
        if keywords:
            blob = f"{job.title} {job.description}".lower()
            hits = sorted(keyword for keyword in keywords if keyword in blob)
            if hits:
                score = max(score, min(1.0, 0.6 + 0.1 * len(hits)))
                notes.append(f"matches target keywords: {', '.join(hits[:5])}")

    return ScoreComponent(
        name="other_preferences",
        raw_score=score,
        weight=payload.weights.other_preferences,
        rationale="; ".join(notes) or "no additional preferences to score",
    )


def total_score(components: list[ScoreComponent]) -> float:
    return round(min(1.0, sum(component.weighted for component in components)), 6)


def derive_uncertainty(
    hard: HardConstraintResult,
    gaps: list[MatchedRequirement],
    payload: ScoringInput,
) -> list[str]:
    """What the system could not determine — shown, never guessed."""
    uncertainty = list(hard.unknown)
    uncertainty.extend(f"unconfirmed: {gap.requirement}" for gap in gaps if gap.satisfied is None)
    if not payload.job.requirements:
        uncertainty.append("posting has no extractable requirements; score is weakly grounded")
    if not any(fact.verified for fact in payload.usable_facts()):
        uncertainty.append("no candidate facts have been verified yet")
    return uncertainty


def infer_requirement_keys(job: Job) -> Job:
    """Backfill normalized keys on requirements that lack one."""
    for requirement in job.requirements:
        if requirement.key is None:
            requirement.key = normalized_key(requirement.text)
    return job
