"""The mock application site.

Exercised directly over HTTP here; M5 drives the same site through a real browser.
Testing it at both levels means a Playwright failure can be attributed to the
automation rather than to the site.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.browser.mock_site.app import app


@pytest.fixture
def site() -> Iterator[TestClient]:
    with TestClient(app) as client:
        client.post("/_reset")
        yield client
        client.post("/_reset")


def _valid_payload() -> dict[str, str]:
    return {
        "full_name": "Priya Raghavan",
        "email": "priya.raghavan@example.com",
        "phone": "+91 98450 12345",
        "location": "Bengaluru, India",
        "work_authorization": "yes",
        "notice_period": "60",
        "terms": "yes",
    }


def _token(site: TestClient, job_id: str = "mock-001") -> str:
    page = site.get(f"/jobs/{job_id}/apply").text
    marker = 'name="form_token" value="'
    start = page.index(marker) + len(marker)
    token: str = page[start : page.index('"', start)]
    return token


class TestFormRendering:
    def test_listing_shows_jobs(self, site: TestClient) -> None:
        body = site.get("/").text
        assert "Senior Machine Learning Engineer" in body

    def test_form_exposes_labelled_fields(self, site: TestClient) -> None:
        """Automation must be able to use semantic labels, not coordinates."""
        body = site.get("/jobs/mock-001/apply").text
        for field in ("full_name", "email", "resume", "work_authorization", "terms"):
            assert f'name="{field}"' in body
        assert "<label" in body

    def test_unknown_question_is_rendered_when_configured(self, site: TestClient) -> None:
        body = site.get("/jobs/mock-002/apply").text
        assert "expected annual compensation" in body.lower()
        assert 'name="compensation"' in body

    def test_captcha_job_shows_a_stop_page_and_no_form(self, site: TestClient) -> None:
        body = site.get("/jobs/mock-003/apply").text
        assert 'data-captcha="required"' in body
        # Nothing to fill in: there is deliberately no bypass to find.
        assert "application-form" not in body


class TestSubmission:
    def test_valid_submission_returns_a_reference(self, site: TestClient) -> None:
        response = site.post(
            "/jobs/mock-001/apply",
            data={**_valid_payload(), "form_token": _token(site)},
            files={"resume": ("resume.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        assert response.status_code == 200
        assert "Application received" in response.text
        assert "MOCK-" in response.text

    def test_site_records_what_it_received(self, site: TestClient) -> None:
        response = site.post(
            "/jobs/mock-001/apply",
            data={**_valid_payload(), "form_token": _token(site)},
            files={"resume": ("tailored.pdf", b"%PDF-1.4 fake", "application/pdf")},
        )
        marker = 'id="confirmation-ref">'
        start = response.text.index(marker) + len(marker)
        reference = response.text[start : response.text.index("<", start)]

        record = site.get(f"/submissions/{reference}").json()
        assert record["found"] is True
        assert record["submission"]["resume_filename"] == "tailored.pdf"
        assert record["submission"]["work_authorization"] == "yes"

    @pytest.mark.parametrize(
        ("field", "value", "expected_error"),
        [
            ("full_name", "", "Full name is required"),
            ("email", "not-an-email", "valid email"),
            ("work_authorization", "", "Work authorization"),
            ("terms", "", "confirm the information"),
        ],
    )
    def test_validation_errors_are_reported(
        self, site: TestClient, field: str, value: str, expected_error: str
    ) -> None:
        payload = {**_valid_payload(), "form_token": _token(site)}
        payload[field] = value
        response = site.post(
            "/jobs/mock-001/apply",
            data=payload,
            files={"resume": ("resume.pdf", b"%PDF", "application/pdf")},
        )
        assert response.status_code == 422
        assert expected_error.lower() in response.text.lower()

    def test_non_pdf_resume_is_rejected(self, site: TestClient) -> None:
        response = site.post(
            "/jobs/mock-001/apply",
            data={**_valid_payload(), "form_token": _token(site)},
            files={"resume": ("resume.docx", b"junk", "application/octet-stream")},
        )
        assert response.status_code == 422
        assert "PDF resume is required" in response.text

    def test_missing_answer_to_an_unknown_question_blocks_submission(
        self, site: TestClient
    ) -> None:
        response = site.post(
            "/jobs/mock-002/apply",
            data={**_valid_payload(), "form_token": _token(site, "mock-002")},
            files={"resume": ("resume.pdf", b"%PDF", "application/pdf")},
        )
        assert response.status_code == 422
        assert "compensation is required" in response.text.lower()


class TestDuplicateSubmitPrevention:
    def test_replaying_a_form_token_is_rejected(self, site: TestClient) -> None:
        """The behaviour the agent's idempotency key pairs with."""
        token = _token(site)
        payload = {**_valid_payload(), "form_token": token}
        files = {"resume": ("resume.pdf", b"%PDF", "application/pdf")}

        first = site.post("/jobs/mock-001/apply", data=payload, files=files)
        assert first.status_code == 200

        second = site.post("/jobs/mock-001/apply", data=payload, files=files)
        assert second.status_code == 409
        assert "already" in second.text.lower()

    def test_a_fresh_form_yields_a_fresh_token(self, site: TestClient) -> None:
        assert _token(site) != _token(site)
