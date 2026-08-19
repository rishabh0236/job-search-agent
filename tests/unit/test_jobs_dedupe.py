"""Deduplication signals (FR-13)."""

from __future__ import annotations

from datetime import UTC, datetime

from packages.schemas.job import Job
from services.jobs import dedupe


def _job(job_id: str, **overrides: object) -> Job:
    payload: dict[str, object] = {
        "id": job_id,
        "source": "local",
        "source_job_id": job_id,
        "company": "Northwind Retail Analytics",
        "title": "Senior Machine Learning Engineer",
        "location": "Bengaluru, India",
        "description": "Build shelf analytics with Python and PyTorch for grocery retailers "
        "across many stores and regions every single week of the year.",
        "retrieved_at": datetime.now(UTC),
    }
    payload.update(overrides)
    return Job.model_validate(payload)


class TestNormalisation:
    def test_company_suffixes_are_dropped(self) -> None:
        assert dedupe.normalize_company(
            "Northwind Retail Analytics Pvt"
        ) == dedupe.normalize_company("Northwind Retail Analytics")

    def test_title_decoration_is_dropped(self) -> None:
        assert dedupe.normalize_title("Senior Engineer (Remote, EU)") == dedupe.normalize_title(
            "Senior Engineer"
        )
        assert dedupe.normalize_title("Backend Engineer (m/f/d)") == dedupe.normalize_title(
            "Backend Engineer"
        )

    def test_tracking_parameters_are_stripped_from_urls(self) -> None:
        left = dedupe.canonical_url("https://Careers.Example.com/jobs/7?utm_source=x&id=7#apply")
        right = dedupe.canonical_url("https://careers.example.com/jobs/7/?id=7&gh_src=y")
        assert left == right

    def test_meaningful_query_parameters_are_kept(self) -> None:
        assert "id=7" in dedupe.canonical_url("https://example.com/j?id=7")

    def test_requisition_id_is_extracted(self) -> None:
        job = _job("a", description="Some text. Requisition ID: RML-001 and more text.")
        assert dedupe.requisition_id(job) == "RML-001"

    def test_absent_requisition_id_is_empty_not_guessed(self) -> None:
        assert dedupe.requisition_id(_job("a", description="no ids here")) == ""


class TestDuplicateDetection:
    def test_same_source_identity(self) -> None:
        duplicate, reason = dedupe.is_duplicate(
            dedupe.DedupeKey.of(_job("a")), dedupe.DedupeKey.of(_job("a"))
        )
        assert duplicate and reason == "source_identity"

    def test_same_canonical_url_across_sources(self) -> None:
        left = _job("a", url="https://x.example/j/1?utm_source=board")
        right = _job("b", source="greenhouse", url="https://x.example/j/1?gh_src=feed")
        duplicate, reason = dedupe.is_duplicate(
            dedupe.DedupeKey.of(left), dedupe.DedupeKey.of(right)
        )
        assert duplicate and reason == "canonical_url"

    def test_same_requisition_id_within_a_company(self) -> None:
        left = _job("a", description="Req ID: ABC-9 plus prose")
        right = _job("b", source="greenhouse", description="Requisition ID: ABC-9 different prose")
        duplicate, reason = dedupe.is_duplicate(
            dedupe.DedupeKey.of(left), dedupe.DedupeKey.of(right)
        )
        assert duplicate and reason == "requisition_id"

    def test_same_requisition_id_at_different_companies_is_not_a_duplicate(self) -> None:
        """Requisition ids are only unique within one company."""
        left = _job("a", description="Req ID: ABC-9")
        right = _job("b", company="Different Corp", description="Req ID: ABC-9")
        duplicate, _ = dedupe.is_duplicate(dedupe.DedupeKey.of(left), dedupe.DedupeKey.of(right))
        assert duplicate is False

    def test_identical_description_at_same_company_and_title(self) -> None:
        duplicate, reason = dedupe.is_duplicate(
            dedupe.DedupeKey.of(_job("a")),
            dedupe.DedupeKey.of(_job("b", source="greenhouse")),
        )
        assert duplicate
        assert reason.startswith("description_similarity")

    def test_different_roles_at_the_same_company_are_not_merged(self) -> None:
        """A false merge hides a job the candidate never sees, so the bar is high."""
        left = _job("a", title="Senior Machine Learning Engineer")
        right = _job(
            "b",
            title="Technical Writer",
            description="Write developer documentation and maintain the style guide "
            "for our public API reference across every product surface.",
        )
        duplicate, _ = dedupe.is_duplicate(dedupe.DedupeKey.of(left), dedupe.DedupeKey.of(right))
        assert duplicate is False

    def test_similarity_of_unrelated_text_is_low(self) -> None:
        assert (
            dedupe.description_similarity(
                "alpha beta gamma delta epsilon", "one two three four five"
            )
            < 0.1
        )


class TestGrouping:
    def test_duplicates_share_a_group(self) -> None:
        jobs = [_job("a"), _job("b", source="greenhouse"), _job("c", title="Data Analyst")]
        groups = dedupe.assign_groups(jobs)
        assert groups["a"] == groups["b"]
        assert groups["c"] != groups["a"]

    def test_grouping_is_stable_for_the_same_input(self) -> None:
        jobs = [_job("a"), _job("b", source="greenhouse")]
        assert dedupe.assign_groups(jobs) == dedupe.assign_groups(jobs)

    def test_every_job_gets_a_group(self) -> None:
        jobs = [_job(str(index), title=f"Role {index}") for index in range(5)]
        assert len(dedupe.assign_groups(jobs)) == 5
