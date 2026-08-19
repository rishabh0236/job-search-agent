"""Deterministic fact extraction and normalization."""

from __future__ import annotations

import pytest

from packages.schemas.enums import FactCategory, SourceType
from packages.schemas.ingestion import ExtractedBlock, ExtractedDocument
from services.candidate import parsing


def _document(*blocks: ExtractedBlock) -> ExtractedDocument:
    return ExtractedDocument(
        source_type=SourceType.TEXT,
        source_path="memory",
        sha256="0" * 64,
        blocks=list(blocks),
    )


def _block(text: str, section: str | None = None, kind: str = "line") -> ExtractedBlock:
    return ExtractedBlock(
        locator=f"line={abs(hash(text)) % 97}", text=text, kind=kind, section=section
    )


class TestContactExtraction:
    def test_email_phone_and_handles(self) -> None:
        document = _document(
            _block(
                "+91 98450 12345 | priya.raghavan@example.com | "
                "linkedin.com/in/priyaraghavan | github.com/praghavan"
            )
        )
        facts = parsing.extract_deterministic_facts(document)
        kinds = {fact.attributes["kind"]: fact.claim for fact in facts}

        assert kinds["email"] == "priya.raghavan@example.com"
        assert kinds["linkedin"] == "linkedin.com/in/priyaraghavan"
        assert kinds["github"] == "github.com/praghavan"
        assert "98450" in kinds["phone"]

    def test_year_range_is_not_mistaken_for_a_phone_number(self) -> None:
        """Regression guard: "2015 -- 2019" has digits but is not a phone."""
        document = _document(_block("B.Tech in Computer Science, 2015 - 2019"))
        facts = parsing.extract_deterministic_facts(document)
        assert not any(fact.attributes.get("kind") == "phone" for fact in facts)

    def test_contacts_are_found_outside_a_labelled_section(self) -> None:
        # Resume headers are rarely under a heading, so contact scanning must not
        # depend on section attribution.
        document = _document(_block("reach me at a@b.io", section=None))
        facts = parsing.extract_deterministic_facts(document)
        assert [fact.claim for fact in facts] == ["a@b.io"]

    def test_duplicate_contacts_are_emitted_once(self) -> None:
        document = _document(_block("a@b.io"), _block("a@b.io again"))
        facts = parsing.extract_deterministic_facts(document)
        assert len(facts) == 1

    def test_phone_confidence_is_below_certain(self) -> None:
        document = _document(_block("+91 98450 12345"))
        phone = next(f for f in parsing.extract_deterministic_facts(document))
        assert phone.confidence < 1.0


class TestSkillExtraction:
    def test_labelled_skill_line_is_split(self) -> None:
        document = _document(_block("Languages: Python, Go, SQL", section="skills"))
        facts = parsing.extract_deterministic_facts(document)

        assert [fact.claim for fact in facts] == ["Python", "Go", "SQL"]
        assert all(fact.category is FactCategory.SKILL for fact in facts)
        assert {fact.attributes["group"] for fact in facts} == {"Languages"}

    def test_label_itself_is_not_a_skill(self) -> None:
        document = _document(_block("Developer Tools: Docker, Terraform", section="skills"))
        claims = [fact.claim for fact in parsing.extract_deterministic_facts(document)]
        assert "Developer Tools" not in claims
        assert claims == ["Docker", "Terraform"]

    def test_prose_outside_the_skills_section_is_not_split(self) -> None:
        """A comma-separated sentence in a bullet is prose, not a skills list."""
        document = _document(
            _block("Designed the billing service in Python, Go and SQL", section="experience")
        )
        assert parsing.extract_deterministic_facts(document) == []

    def test_aliases_are_canonicalised(self) -> None:
        document = _document(_block("k8s, js, postgres, sklearn", section="skills"))
        claims = [fact.claim for fact in parsing.extract_deterministic_facts(document)]
        assert claims == ["Kubernetes", "JavaScript", "PostgreSQL", "scikit-learn"]

    @pytest.mark.parametrize("raw", ["", "a", "-", "   ", "skills"])
    def test_implausible_skills_are_rejected(self, raw: str) -> None:
        assert parsing.normalize_skill(raw) is None

    def test_unknown_skill_keeps_the_candidates_capitalisation(self) -> None:
        # Rewriting "Kubeflow" to "kubeflow" would be an unrequested edit to the
        # candidate's own words.
        assert parsing.normalize_skill("Kubeflow") == "Kubeflow"

    def test_long_phrase_is_not_a_skill(self) -> None:
        assert parsing.normalize_skill("built a really large distributed system") is None


class TestDateRanges:
    @pytest.mark.parametrize(
        ("text", "start", "end"),
        [
            ("March 2022 -- Present", "March 2022", "present"),
            ("July 2019 - February 2022", "July 2019", "February 2022"),
            ("2015 – 2019", "2015", "2019"),  # noqa: RUF001 - en dash is the case under test
            ("Jan 2020 to Dec 2021", "Jan 2020", "Dec 2021"),
        ],
    )
    def test_parses_common_formats(self, text: str, start: str, end: str) -> None:
        parsed = parsing.parse_date_range(text)
        assert parsed is not None
        assert parsed["start_date"] == start
        assert parsed["end_date"] == end

    def test_current_role_is_flagged(self) -> None:
        parsed = parsing.parse_date_range("March 2022 -- Present")
        assert parsed is not None
        assert parsed["is_current"] == "true"

    def test_no_range_returns_none(self) -> None:
        assert parsing.parse_date_range("Senior Engineer, Bengaluru") is None

    def test_single_date_is_not_invented_into_a_range(self) -> None:
        """An open-ended date must not silently become "to present"."""
        assert parsing.parse_date_range("Started March 2022") is None
