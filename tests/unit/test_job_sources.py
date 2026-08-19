"""Job source adapters (skills/02) — normalization and pacing, no real network.

Every adapter is exercised against an ``httpx.MockTransport`` so these stay fast,
offline and deterministic while still proving the adapter parses a realistic
payload shape correctly.
"""

from __future__ import annotations

from typing import Any

import httpx

from packages.schemas.enums import EmploymentType, RemoteMode
from packages.schemas.job import JobSearchCriteria
from services.jobs.sources import (
    AdzunaSource,
    ArbeitnowSource,
    AshbySource,
    CareerPageSource,
    LeverSource,
    SmartRecruitersSource,
    infer_employment_type,
)


def _client(handler: Any) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestInferEmploymentType:
    def test_full_time_is_detected(self) -> None:
        assert infer_employment_type("Full-time") is EmploymentType.FULL_TIME

    def test_international_does_not_match_internship(self) -> None:
        """Regression: a live SmartRecruiters GRC posting ("international
        compliance", "internal audits") was misclassified as an internship
        because "intern" is a substring of both words.
        """
        text = "Ensure compliance with international regulations and internal audits."
        assert infer_employment_type(text) is EmploymentType.UNKNOWN

    def test_actual_internship_is_still_detected(self) -> None:
        assert infer_employment_type("Summer internship program") is EmploymentType.INTERNSHIP


class TestLeverSource:
    def _payload(self) -> list[dict[str, Any]]:
        return [
            {
                "id": "lever-1",
                "text": "Backend Engineer",
                "categories": {"location": "Bengaluru", "commitment": "Full-time"},
                "descriptionPlain": "Build APIs.",
                "lists": [{"text": "Requirements", "content": "<ul><li>Python</li></ul>"}],
                "hostedUrl": "https://jobs.lever.co/acme/lever-1",
                "createdAt": 1_700_000_000_000,
            }
        ]

    def test_search_normalizes_and_filters(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v0/postings/acme"
            return httpx.Response(200, json=self._payload())

        source = LeverSource(["acme"], client=_client(handler), request_delay_seconds=0)
        jobs = source.search(JobSearchCriteria(limit=10))

        assert len(jobs) == 1
        job = jobs[0]
        assert job.source == "lever"
        assert job.source_job_id == "lever-1"
        assert job.company == "acme"
        assert job.title == "Backend Engineer"
        assert job.employment_type is EmploymentType.FULL_TIME
        assert str(job.url) == "https://jobs.lever.co/acme/lever-1"
        assert "Python" in job.description

    def test_unreachable_site_does_not_fail_the_whole_search(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        source = LeverSource(["broken"], client=_client(handler), request_delay_seconds=0)
        assert source.search(JobSearchCriteria(limit=10)) == []

    def test_health_check_without_sites(self) -> None:
        source = LeverSource([], client=_client(lambda r: httpx.Response(200, json=[])))
        health = source.health_check()
        assert health.healthy is False


class TestAshbySource:
    def _payload(self) -> dict[str, Any]:
        return {
            "jobs": [
                {
                    "id": "ashby-1",
                    "title": "Data Scientist",
                    "location": "Remote - India",
                    "isRemote": True,
                    "employmentType": "FullTime",
                    "descriptionPlain": "Model things.",
                    "jobUrl": "https://jobs.ashbyhq.com/acme/ashby-1",
                    "publishedAt": "2024-01-01T00:00:00.000Z",
                }
            ]
        }

    def test_search_normalizes(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/posting-api/job-board/acme"
            return httpx.Response(200, json=self._payload())

        source = AshbySource(["acme"], client=_client(handler), request_delay_seconds=0)
        jobs = source.search(JobSearchCriteria(limit=10))

        assert len(jobs) == 1
        job = jobs[0]
        assert job.source == "ashby"
        assert job.remote is RemoteMode.REMOTE
        assert job.employment_type is EmploymentType.FULL_TIME
        assert job.posted_at is not None

    def test_fetch_filters_by_id_from_the_listing(self) -> None:
        source = AshbySource(
            ["acme"],
            client=_client(lambda r: httpx.Response(200, json=self._payload())),
            request_delay_seconds=0,
        )
        job = source.fetch("ashby-1")
        assert job is not None
        assert job.source_job_id == "ashby-1"
        assert source.fetch("missing") is None


class TestSmartRecruitersSource:
    def test_search_fetches_detail_per_posting(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/postings"):
                return httpx.Response(
                    200,
                    json={
                        "content": [
                            {
                                "id": "sr-1",
                                "name": "Platform Engineer",
                                "location": {
                                    "city": "Pune",
                                    "country": "India",
                                    "remote": False,
                                    "hybrid": False,
                                },
                                "company": {"name": "Acme Inc"},
                                "releasedDate": "2024-02-01T00:00:00Z",
                                "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
                            }
                        ]
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": "sr-1",
                    "postingUrl": "https://jobs.smartrecruiters.com/acme/sr-1-platform-engineer",
                    "jobAd": {
                        "sections": {
                            "jobDescription": {"text": "<p>Own the platform.</p>"},
                        }
                    },
                },
            )

        source = SmartRecruitersSource(["acme"], client=_client(handler), request_delay_seconds=0)
        jobs = source.search(JobSearchCriteria(limit=10))

        assert len(jobs) == 1
        job = jobs[0]
        assert job.source == "smartrecruiters"
        assert job.company == "Acme Inc"
        assert job.employment_type is EmploymentType.FULL_TIME
        assert "Own the platform" in job.description
        assert str(job.url) == "https://jobs.smartrecruiters.com/acme/sr-1-platform-engineer"


class TestAdzunaSource:
    def test_search_normalizes_and_builds_salary(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.params["app_id"] == "id123"
            return httpx.Response(
                200,
                json={
                    "results": [
                        {
                            "id": 42,
                            "title": "ML Engineer",
                            "company": {"display_name": "Acme"},
                            "location": {"display_name": "Bengaluru, India"},
                            "description": "Ship models.",
                            "redirect_url": "https://adzuna.example/job/42",
                            "salary_min": 1_500_000,
                            "salary_max": 2_500_000,
                            "contract_time": "full_time",
                            "created": "2024-03-01T00:00:00Z",
                        }
                    ]
                },
            )

        source = AdzunaSource(
            "id123", "key456", country="in", client=_client(handler), request_delay_seconds=0
        )
        jobs = source.search(
            JobSearchCriteria(titles=["ML Engineer"], locations=["Bengaluru"], limit=10)
        )

        assert len(jobs) == 1
        job = jobs[0]
        assert job.source == "adzuna"
        assert job.salary is not None
        assert job.salary.currency == "INR"
        assert job.employment_type is EmploymentType.FULL_TIME

    def test_fetch_is_unsupported_by_the_public_api(self) -> None:
        source = AdzunaSource("id", "key", client=_client(lambda r: httpx.Response(200, json={})))
        assert source.fetch("anything") is None


class TestArbeitnowSource:
    def test_search_paginates_until_no_next_link(self) -> None:
        pages = [
            {
                "data": [
                    {"slug": "job-1", "company_name": "Acme", "title": "Engineer", "remote": True}
                ],
                "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"},
            },
            {
                "data": [{"slug": "job-2", "company_name": "Acme", "title": "Engineer II"}],
                "links": {},
            },
        ]

        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            response = httpx.Response(200, json=pages[calls["n"]])
            calls["n"] += 1
            return response

        source = ArbeitnowSource(client=_client(handler), request_delay_seconds=0)
        jobs = source.search(JobSearchCriteria(limit=10))

        assert {job.source_job_id for job in jobs} == {"job-1", "job-2"}
        assert calls["n"] == 2

    def test_health_check_reports_count(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": [{"slug": "a"}, {"slug": "b"}], "links": {}})

        source = ArbeitnowSource(client=_client(handler), request_delay_seconds=0)
        health = source.health_check()
        assert health.healthy is True
        assert "2" in (health.detail or "")


def _robots_ok(request: httpx.Request) -> httpx.Response | None:
    if request.url.path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nAllow: /")
    return None


class TestCareerPageSource:
    def test_direct_json_ld_on_start_page(self) -> None:
        start_url = "https://careers.acme.example/jobs/backend-engineer"
        html = """
        <html><body>
        <script type="application/ld+json">
        {
            "@type": "JobPosting",
            "title": "Backend Engineer",
            "hiringOrganization": {"name": "Acme Inc"},
            "jobLocation": {"address": {"addressLocality": "Bengaluru", "addressCountry": "IN"}},
            "employmentType": "FULL_TIME",
            "jobLocationType": "TELECOMMUTE",
            "datePosted": "2024-01-01",
            "baseSalary": {
                "currency": "INR",
                "value": {"minValue": 1500000, "maxValue": 2500000, "unitText": "YEAR"}
            },
            "description": "<p>Build things.</p>"
        }
        </script>
        </body></html>
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return _robots_ok(request) or httpx.Response(200, text=html)

        source = CareerPageSource([start_url], client=_client(handler), request_delay_seconds=0)
        jobs = source.search(JobSearchCriteria(limit=10))

        assert len(jobs) == 1
        job = jobs[0]
        assert job.source == "career_page"
        assert job.title == "Backend Engineer"
        assert job.company == "Acme Inc"
        assert job.location == "Bengaluru, IN"
        assert job.remote is RemoteMode.REMOTE
        assert job.employment_type is EmploymentType.FULL_TIME
        assert job.salary is not None
        assert job.salary.currency == "INR"
        assert job.salary.period == "year"
        assert str(job.url) == start_url

    def test_free_text_employment_type_falls_back_to_word_boundary_match(self) -> None:
        """Regression: a live Lever-hosted page embeds employmentType as free text
        ("Regular Full Time (Salary)"), not Google's enum vocabulary.
        """
        start_url = "https://careers.acme.example/jobs/role"
        html = """
        <script type="application/ld+json">
        {
            "@type": "JobPosting",
            "title": "Role",
            "hiringOrganization": {"name": "Acme"},
            "employmentType": "Regular Full Time (Salary)"
        }
        </script>
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return _robots_ok(request) or httpx.Response(200, text=html)

        source = CareerPageSource([start_url], client=_client(handler), request_delay_seconds=0)
        jobs = source.search(JobSearchCriteria(limit=10))
        assert jobs[0].employment_type is EmploymentType.FULL_TIME

    def test_robots_disallow_blocks_the_source_entirely(self) -> None:
        start_url = "https://careers.acme.example/jobs"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nDisallow: /")
            return httpx.Response(200, text="<html></html>")

        source = CareerPageSource([start_url], client=_client(handler), request_delay_seconds=0)
        assert source.search(JobSearchCriteria(limit=10)) == []
        health = source.health_check()
        assert health.healthy is False
        assert "robots.txt" in (health.detail or "")

    def test_follows_job_shaped_links_and_ignores_domain_substring(self) -> None:
        """The host itself is "jobs.*", so a naive full-URL substring check would
        wrongly treat every same-domain link as job-shaped. "/about" must be
        skipped; only the "/careers/..." link is job-shaped by its path.
        """
        start_url = "https://jobs.acme.example/careers"
        posting_url = "https://jobs.acme.example/careers/senior-engineer-123"
        listing_html = f"""
        <html><body>
        <a href="/about">About</a>
        <a href="{posting_url}">Senior Engineer</a>
        </body></html>
        """
        posting_html = """
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "Senior Engineer", "hiringOrganization": {"name": "Acme"}}
        </script>
        """
        pages = {start_url: listing_html, posting_url: posting_html}
        visited: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            robots = _robots_ok(request)
            if robots is not None:
                return robots
            url = str(request.url)
            visited.append(url)
            body = pages.get(url)
            return httpx.Response(200, text=body) if body is not None else httpx.Response(404)

        source = CareerPageSource([start_url], client=_client(handler), request_delay_seconds=0)
        jobs = source.search(JobSearchCriteria(limit=10))

        assert len(jobs) == 1
        assert jobs[0].title == "Senior Engineer"
        assert str(jobs[0].url) == posting_url
        assert "https://jobs.acme.example/about" not in visited

    def test_link_following_is_bounded(self) -> None:
        start_url = "https://careers.acme.example/jobs"
        links_html = "".join(f'<a href="/jobs/{i}">Job {i}</a>' for i in range(10))
        listing_html = f"<html><body>{links_html}</body></html>"
        posting_html = """
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "Role", "hiringOrganization": {"name": "Acme"}}
        </script>
        """
        fetched_job_pages: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            robots = _robots_ok(request)
            if robots is not None:
                return robots
            if request.url.path == "/jobs":
                return httpx.Response(200, text=listing_html)
            fetched_job_pages.append(request.url.path)
            return httpx.Response(200, text=posting_html)

        source = CareerPageSource(
            [start_url], client=_client(handler), request_delay_seconds=0, max_links_per_page=3
        )
        jobs = source.search(JobSearchCriteria(limit=100))

        assert len(fetched_job_pages) == 3
        assert len(jobs) == 3

    def test_fetch_reads_the_url_directly(self) -> None:
        posting_url = "https://careers.acme.example/jobs/role"
        html = """
        <script type="application/ld+json">
        {"@type": "JobPosting", "title": "Role", "hiringOrganization": {"name": "Acme"}}
        </script>
        """

        def handler(request: httpx.Request) -> httpx.Response:
            return _robots_ok(request) or httpx.Response(200, text=html)

        source = CareerPageSource([], client=_client(handler), request_delay_seconds=0)
        job = source.fetch(posting_url)
        assert job is not None
        assert job.title == "Role"
        assert source.fetch("not-a-url") is None

    def test_health_check_without_pages(self) -> None:
        source = CareerPageSource([], client=_client(lambda r: httpx.Response(200)))
        assert source.health_check().healthy is False
