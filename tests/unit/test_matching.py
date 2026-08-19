"""Scoring, hard filters and retrieval.

The rule these tests exist to protect: a missing fact is an unknown to ask about,
never a negative to score against.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from packages.schemas.candidate import CandidateFact, CandidatePreferences, TargetRole
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
from packages.schemas.matching import ScoreWeights
from services.matching import retrieval, scoring


def _fact(
    claim: str,
    category: FactCategory = FactCategory.SKILL,
    *,
    provenance: Provenance = Provenance.RESUME,
    attributes: dict[str, object] | None = None,
) -> CandidateFact:
    return CandidateFact(
        id=f"fact_{abs(hash(claim)) % 10**8}",
        candidate_id="cand_1",
        category=category,
        claim=claim,
        attributes=attributes or {},
        evidence=[
            EvidenceRef(
                evidence_id=f"ev_{abs(hash(claim)) % 10**8}",
                source_id="res_1",
                locator="line=1",
                quote=claim,
            )
        ],
        confidence=0.9,
        provenance=provenance,
        verified=True,
    )


def _job(**overrides: object) -> Job:
    payload: dict[str, object] = {
        "id": "job_1",
        "source": "local",
        "source_job_id": "1",
        "company": "Northwind",
        "title": "Senior Machine Learning Engineer",
        "location": "Bengaluru, India",
        "remote": RemoteMode.HYBRID,
        "employment_type": EmploymentType.FULL_TIME,
        "description": "Requirements\n- Strong Python and PyTorch\n- Docker and Kubernetes\n",
        "requirements": [
            JobRequirement(
                text="Strong Python and PyTorch", kind=RequirementKind.REQUIRED, key="python"
            ),
            JobRequirement(
                text="Docker and Kubernetes", kind=RequirementKind.REQUIRED, key="kubernetes"
            ),
        ],
        "retrieved_at": datetime.now(UTC),
    }
    payload.update(overrides)
    return Job.model_validate(payload)


def _input(
    facts: list[CandidateFact] | None = None,
    preferences: CandidatePreferences | None = None,
    job: Job | None = None,
) -> scoring.ScoringInput:
    return scoring.ScoringInput(
        job=job or _job(),
        facts=facts if facts is not None else [_fact("Python"), _fact("Kubernetes")],
        preferences=preferences or CandidatePreferences(),
        weights=ScoreWeights(),
    )


class TestRetrieval:
    def test_related_text_scores_above_unrelated(self) -> None:
        payload = "Built computer vision models for retail shelf recognition"
        related = retrieval.text_similarity(payload, "computer vision retail shelf models")
        unrelated = retrieval.text_similarity(payload, "corporate tax filing and payroll")
        assert related > unrelated

    def test_index_returns_best_match_first(self) -> None:
        index = retrieval.InMemoryIndex()
        index.add("a", "Designed the billing reconciliation service in Python")
        index.add("b", "Trained detection models with PyTorch")
        results = index.query("PyTorch model training", top_k=2)
        assert results[0].item_id == "b"

    def test_empty_query_returns_nothing(self) -> None:
        index = retrieval.InMemoryIndex()
        index.add("a", "Python")
        assert index.query("the and of") == []

    def test_stopwords_do_not_create_similarity(self) -> None:
        assert retrieval.text_similarity("the and of it", "the and of it is") <= 1.0
        assert retrieval.text_similarity("we are a team", "of the and") == 0.0


class TestHardConstraints:
    def test_all_clear_is_eligible(self) -> None:
        preferences = CandidatePreferences(
            remote_modes=[RemoteMode.HYBRID], employment_types=[EmploymentType.FULL_TIME]
        )
        result = scoring.evaluate_hard_constraints(_input(preferences=preferences))
        assert result.eligibility is Eligibility.ELIGIBLE
        assert result.blocking == []

    def test_explicit_exclusion_blocks(self) -> None:
        preferences = CandidatePreferences(exclusions=["gambling"])
        job = _job(description="We are a gambling technology company.\nRequirements\n- Python\n")
        result = scoring.evaluate_hard_constraints(_input(preferences=preferences, job=job))
        assert result.eligibility is Eligibility.INELIGIBLE
        assert any("gambling" in reason for reason in result.blocking)

    def test_unstated_years_is_unknown_not_ineligible(self) -> None:
        """The core rule: absent data must never manufacture a rejection."""
        job = _job(
            requirements=[
                JobRequirement(
                    text="10+ years of experience",
                    kind=RequirementKind.REQUIRED,
                    key="years_experience>=10",
                )
            ]
        )
        result = scoring.evaluate_hard_constraints(_input(job=job))
        assert result.eligibility is Eligibility.UNKNOWN
        assert result.blocking == []
        assert any("does not state a total" in item for item in result.unknown)

    def test_stated_years_far_below_requirement_blocks(self) -> None:
        job = _job(
            requirements=[
                JobRequirement(
                    text="10+ years of experience",
                    kind=RequirementKind.REQUIRED,
                    key="years_experience>=10",
                )
            ]
        )
        facts = [
            _fact("Six years of engineering", FactCategory.EXPERIENCE, attributes={"years": "6"})
        ]
        result = scoring.evaluate_hard_constraints(_input(facts=facts, job=job))
        assert result.eligibility is Eligibility.INELIGIBLE

    def test_one_year_short_is_tolerated(self) -> None:
        """A "5 years" posting routinely accepts 4; a hard reject hides good jobs."""
        job = _job(
            requirements=[
                JobRequirement(
                    text="5+ years", kind=RequirementKind.REQUIRED, key="years_experience>=5"
                )
            ]
        )
        facts = [_fact("Four years", FactCategory.EXPERIENCE, attributes={"years": "4"})]
        result = scoring.evaluate_hard_constraints(_input(facts=facts, job=job))
        assert result.eligibility is not Eligibility.INELIGIBLE

    def test_work_authorization_mention_creates_an_unknown(self) -> None:
        job = _job(description="Right to work in the United Kingdom is required.\n")
        result = scoring.evaluate_hard_constraints(_input(job=job))
        assert any("work authorization" in item for item in result.unknown)
        assert result.blocking == []

    def test_confirmed_authorization_clears_the_unknown(self) -> None:
        job = _job(description="Right to work in the United Kingdom is required.\n")
        facts = [_fact("Holds UK work permit", FactCategory.WORK_AUTHORIZATION)]
        result = scoring.evaluate_hard_constraints(_input(facts=facts, job=job))
        assert not any("work authorization" in item for item in result.unknown)


class TestScoreComponents:
    def test_all_seven_components_are_present(self) -> None:
        payload = _input()
        hard = scoring.evaluate_hard_constraints(payload)
        components, _, _ = scoring.score_components(payload, hard)
        assert {component.name for component in components} == set(ScoreWeights().as_dict())

    def test_total_is_reconstructable_from_components(self) -> None:
        payload = _input()
        hard = scoring.evaluate_hard_constraints(payload)
        components, _, _ = scoring.score_components(payload, hard)
        total = scoring.total_score(components)
        assert total == pytest.approx(sum(c.raw_score * c.weight for c in components), abs=1e-6)

    def test_matching_skills_produce_strengths_with_evidence(self) -> None:
        payload = _input()
        hard = scoring.evaluate_hard_constraints(payload)
        _, strengths, gaps = scoring.score_components(payload, hard)
        assert strengths
        assert all(item.evidence for item in strengths)
        assert not [gap for gap in gaps if gap.satisfied is False]

    def test_missing_skill_is_a_gap_not_a_crash(self) -> None:
        payload = _input(facts=[_fact("Python")])
        hard = scoring.evaluate_hard_constraints(payload)
        _, strengths, gaps = scoring.score_components(payload, hard)
        assert any("Kubernetes" in gap.requirement for gap in gaps)
        assert any("Python" in item.requirement for item in strengths)

    def test_unknown_provenance_facts_do_not_score(self) -> None:
        """An unconfirmed observation must not be why a job ranks highly."""
        unconfirmed = _fact("Kubernetes", provenance=Provenance.UNKNOWN)
        with_unknown = _input(facts=[_fact("Python"), unconfirmed])
        without = _input(facts=[_fact("Python")])

        hard_a = scoring.evaluate_hard_constraints(with_unknown)
        hard_b = scoring.evaluate_hard_constraints(without)
        score_a = scoring.total_score(scoring.score_components(with_unknown, hard_a)[0])
        score_b = scoring.total_score(scoring.score_components(without, hard_b)[0])
        assert score_a == score_b

    def test_a_posting_with_no_requirements_scores_neutrally(self) -> None:
        payload = _input(job=_job(requirements=[], description="Join our team."))
        hard = scoring.evaluate_hard_constraints(payload)
        components, _, _ = scoring.score_components(payload, hard)
        required = next(c for c in components if c.name == "required_skill_evidence")
        assert required.raw_score == 0.5
        assert "no requirements" in required.rationale

    def test_better_candidate_scores_higher(self) -> None:
        strong = _input(
            facts=[
                _fact("Python"),
                _fact("Kubernetes"),
                _fact(
                    "Trained PyTorch detection models for retail shelves", FactCategory.ACHIEVEMENT
                ),
            ]
        )
        weak = _input(facts=[_fact("Microsoft Excel")])
        hard_strong = scoring.evaluate_hard_constraints(strong)
        hard_weak = scoring.evaluate_hard_constraints(weak)
        assert scoring.total_score(
            scoring.score_components(strong, hard_strong)[0]
        ) > scoring.total_score(scoring.score_components(weak, hard_weak)[0])

    def test_every_component_carries_a_rationale(self) -> None:
        payload = _input()
        hard = scoring.evaluate_hard_constraints(payload)
        components, _, _ = scoring.score_components(payload, hard)
        assert all(component.rationale for component in components)


class TestSeniorityAndPreferences:
    def test_matching_seniority_scores_well(self) -> None:
        preferences = CandidatePreferences(
            target_roles=[TargetRole(title="Senior ML Engineer", seniority="senior")]
        )
        payload = _input(preferences=preferences)
        component = scoring._seniority_component(payload)
        assert component.raw_score == 1.0

    def test_distant_seniority_scores_lower(self) -> None:
        preferences = CandidatePreferences(
            target_roles=[TargetRole(title="Junior Engineer", seniority="junior")]
        )
        payload = _input(preferences=preferences, job=_job(title="VP of Engineering"))
        assert scoring._seniority_component(payload).raw_score < 0.5

    def test_unstated_seniority_is_neutral_not_zero(self) -> None:
        payload = _input(job=_job(title="Engineer"))
        assert scoring._seniority_component(payload).raw_score == 0.5

    def test_salary_below_minimum_scores_zero(self) -> None:
        from packages.schemas.job import SalaryRange

        preferences = CandidatePreferences(min_salary=5_000_000)
        job = _job(salary=SalaryRange(min_amount=1_000_000, max_amount=2_000_000, currency="INR"))
        component = scoring._other_preferences_component(_input(preferences=preferences, job=job))
        assert component.raw_score == 0.0

    def test_unadvertised_salary_is_neutral(self) -> None:
        preferences = CandidatePreferences(min_salary=5_000_000)
        component = scoring._other_preferences_component(_input(preferences=preferences))
        assert component.raw_score == 0.5
        assert "does not advertise" in component.rationale


class TestUncertainty:
    def test_unknowns_are_reported(self) -> None:
        payload = _input(job=_job(description="Right to work required.\n"))
        hard = scoring.evaluate_hard_constraints(payload)
        _, _, gaps = scoring.score_components(payload, hard)
        uncertainty = scoring.derive_uncertainty(hard, gaps, payload)
        assert any("work authorization" in item for item in uncertainty)

    def test_unverified_profile_is_flagged(self) -> None:
        unverified = _fact("Python")
        unverified.verified = False
        payload = _input(facts=[unverified])
        hard = scoring.evaluate_hard_constraints(payload)
        _, _, gaps = scoring.score_components(payload, hard)
        assert any(
            "no candidate facts have been verified" in item
            for item in scoring.derive_uncertainty(hard, gaps, payload)
        )
