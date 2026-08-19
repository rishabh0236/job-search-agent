"""Fact validation — the enforcement point for evidence grounding.

Each test here corresponds to a way a resume can be falsified. They are the reason
the rest of the pipeline can be trusted, so they assert on *rejection*, not on
graceful handling.
"""

from __future__ import annotations

from packages.core.db.models import Evidence
from packages.schemas.enums import FactCategory, Provenance
from packages.schemas.ingestion import IngestionFinding
from services.candidate import facts as facts_service


def _evidence(evidence_id: str, quote: str) -> Evidence:
    return Evidence(
        id=evidence_id,
        candidate_id="cand_1",
        source_id="res_1",
        locator="line=10",
        quote=quote,
    )


BULLET = "Led the redesign of the shelf-recognition inference pipeline, cutting p99 latency by 35%."
ROLE = (
    "Senior Machine Learning Engineer March 2022 - Present Infilect Technologies Bengaluru, India"
)

AVAILABLE = {
    "ev_bullet": _evidence("ev_bullet", BULLET),
    "ev_role": _evidence("ev_role", ROLE),
}


def _validate(
    *,
    category: FactCategory = FactCategory.ACHIEVEMENT,
    claim: str = BULLET,
    evidence_ids: list[str] | None = None,
    attributes: dict[str, str] | None = None,
    confidence: float = 0.95,
) -> tuple[facts_service.ValidatedFact | None, list[IngestionFinding]]:
    """Validate a fact, defaulting to a faithful restatement of the bullet."""
    return facts_service.validate(
        category=category,
        claim=claim,
        evidence_ids=["ev_bullet"] if evidence_ids is None else evidence_ids,
        attributes=attributes or {},
        confidence=confidence,
        available_evidence=AVAILABLE,
    )


class TestAcceptance:
    def test_faithful_restatement_is_accepted(self) -> None:
        fact, findings = _validate()
        assert fact is not None
        assert fact.provenance is Provenance.RESUME
        assert findings == []

    def test_supported_metric_is_allowed(self) -> None:
        fact, _ = _validate(claim="Cut p99 latency by 35% on the inference pipeline")
        assert fact is not None

    def test_supported_employer_is_allowed(self) -> None:
        fact, _ = _validate(
            category=FactCategory.EXPERIENCE,
            claim="Senior Machine Learning Engineer",
            evidence_ids=["ev_role"],
            attributes={"employer": "Infilect Technologies"},
        )
        assert fact is not None

    def test_accepted_facts_still_start_unverified(self) -> None:
        """Even a perfect extraction needs human confirmation before use."""
        fact, _ = _validate()
        assert fact is not None
        assert fact.verified is False

    def test_high_risk_confidence_is_capped(self) -> None:
        fact, _ = _validate(
            category=FactCategory.EXPERIENCE, evidence_ids=["ev_bullet"], confidence=1.0
        )
        assert fact is not None
        assert fact.confidence <= 0.9


class TestRejection:
    def test_fabricated_evidence_id_is_rejected(self) -> None:
        fact, findings = _validate(evidence_ids=["ev_does_not_exist"])
        assert fact is None
        assert [f.code for f in findings] == ["fabricated_evidence"]

    def test_invented_metric_is_rejected(self) -> None:
        fact, findings = _validate(claim="Improved throughput by 250% and saved $2M annually")
        assert fact is None
        assert findings[0].code == "unsupported_metric"

    def test_inflated_metric_is_rejected(self) -> None:
        """35% in the source does not license 85% in the claim."""
        fact, _ = _validate(claim="Cut p99 latency by 85%")
        assert fact is None

    def test_invented_year_is_rejected(self) -> None:
        fact, findings = _validate(
            category=FactCategory.EDUCATION,
            claim="Completed the programme in 2011",
            evidence_ids=["ev_role"],
        )
        assert fact is None
        assert findings[0].code == "unsupported_date"

    def test_year_present_in_evidence_is_allowed(self) -> None:
        fact, _ = _validate(
            category=FactCategory.EXPERIENCE,
            claim="In role since March 2022",
            evidence_ids=["ev_role"],
        )
        assert fact is not None

    def test_invented_employer_is_rejected(self) -> None:
        fact, findings = _validate(
            category=FactCategory.EXPERIENCE,
            claim="Senior Engineer",
            evidence_ids=["ev_role"],
            attributes={"employer": "Initech Global"},
        )
        assert fact is None
        assert findings[0].code == "unsupported_entity"

    def test_invented_institution_is_rejected(self) -> None:
        fact, _ = _validate(
            category=FactCategory.EDUCATION,
            claim="Bachelor of Technology",
            evidence_ids=["ev_role"],
            attributes={"institution": "Massachusetts Institute of Technology"},
        )
        assert fact is None

    def test_non_entity_attributes_are_not_entity_checked(self) -> None:
        # "title" is not in ENTITY_ATTRIBUTES: a role title is a restatement of the
        # claim, not an independent real-world entity to verify.
        fact, _ = _validate(
            category=FactCategory.EXPERIENCE,
            claim="Senior Machine Learning Engineer",
            evidence_ids=["ev_role"],
            attributes={"title": "Senior Machine Learning Engineer"},
        )
        assert fact is not None


class TestUnknownHandling:
    def test_claim_without_evidence_becomes_unknown_not_fact(self) -> None:
        fact, findings = _validate(claim="Strong leadership skills", evidence_ids=[])
        assert fact is not None
        assert fact.provenance is Provenance.UNKNOWN
        assert fact.confidence == 0.0
        assert findings[0].code == "no_evidence"

    def test_unknown_facts_are_not_usable_in_artifacts(self) -> None:
        fact, _ = _validate(claim="Strong leadership skills", evidence_ids=[])
        assert fact is not None
        # Provenance UNKNOWN is what downstream tailoring checks before using text.
        assert fact.provenance is Provenance.UNKNOWN
        assert fact.verified is False


class TestFactIdentity:
    def test_ids_are_stable_for_the_same_claim(self) -> None:
        first = facts_service.fact_id_for("cand_1", FactCategory.SKILL, "Python")
        second = facts_service.fact_id_for("cand_1", FactCategory.SKILL, "Python")
        assert first == second

    def test_ids_ignore_punctuation_and_case(self) -> None:
        """Re-ingestion must not duplicate a fact over trivial text differences."""
        assert facts_service.fact_id_for(
            "cand_1", FactCategory.SKILL, "Python."
        ) == facts_service.fact_id_for("cand_1", FactCategory.SKILL, "python")

    def test_ids_differ_across_candidates_and_categories(self) -> None:
        assert facts_service.fact_id_for(
            "cand_1", FactCategory.SKILL, "Python"
        ) != facts_service.fact_id_for("cand_2", FactCategory.SKILL, "Python")
        assert facts_service.fact_id_for(
            "cand_1", FactCategory.SKILL, "Python"
        ) != facts_service.fact_id_for("cand_1", FactCategory.PROJECT, "Python")
