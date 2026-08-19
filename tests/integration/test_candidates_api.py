"""Candidate API behaviour, including the review/correction loop."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from packages.schemas.enums import Provenance


def _create(client: TestClient, name: str = "API Candidate") -> str:
    response = client.post("/candidates", json={"display_name": name})
    assert response.status_code == 201
    candidate_id: str = response.json()["id"]
    return candidate_id


def _upload(client: TestClient, candidate_id: str, path: Path) -> dict[str, Any]:
    with path.open("rb") as handle:
        response = client.post(
            f"/candidates/{candidate_id}/resumes",
            files={"file": (path.name, handle, "application/x-tex")},
        )
    assert response.status_code == 201, response.text
    payload: dict[str, Any] = response.json()
    return payload


class TestCandidateLifecycle:
    def test_create_and_fetch_profile(self, client: TestClient) -> None:
        candidate_id = _create(client)
        profile = client.get(f"/candidates/{candidate_id}").json()

        assert profile["id"] == candidate_id
        assert profile["display_name"] == "API Candidate"
        assert profile["facts"] == []
        # A new candidate has empty preferences, not absent ones.
        assert profile["preferences"]["target_roles"] == []

    def test_missing_candidate_is_404_with_a_typed_error(self, client: TestClient) -> None:
        response = client.get("/candidates/cand_nope")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"

    def test_preferences_round_trip(self, client: TestClient) -> None:
        candidate_id = _create(client)
        response = client.put(
            f"/candidates/{candidate_id}/preferences",
            json={
                "target_roles": [{"title": "Staff Engineer", "keywords": ["python"]}],
                "locations": ["Remote (India)"],
                "remote_modes": ["remote"],
                "min_salary": 4000000,
                "salary_currency": "INR",
            },
        )
        assert response.status_code == 200

        preferences = response.json()["preferences"]
        assert preferences["target_roles"][0]["title"] == "Staff Engineer"
        assert preferences["min_salary"] == 4000000
        # Preferences must not leak into facts (FR-06).
        assert response.json()["facts"] == []

    def test_invalid_preferences_are_rejected(self, client: TestClient) -> None:
        candidate_id = _create(client)
        response = client.put(
            f"/candidates/{candidate_id}/preferences",
            json={"min_salary": -1},
        )
        assert response.status_code == 422


class TestResumeUpload:
    def test_upload_produces_an_ingestion_report(
        self, client: TestClient, sample_tex_path: Path
    ) -> None:
        candidate_id = _create(client)
        report = _upload(client, candidate_id, sample_tex_path)

        assert report["is_original"] is True
        assert report["facts_created"] > 0
        assert report["evidence_count"] == report["block_count"]
        assert "experience" in report["sections"]

    def test_uploaded_facts_appear_on_the_profile_with_evidence(
        self, client: TestClient, sample_tex_path: Path
    ) -> None:
        candidate_id = _create(client)
        _upload(client, candidate_id, sample_tex_path)

        profile = client.get(f"/candidates/{candidate_id}").json()
        assert profile["facts"]

        emails = [f for f in profile["facts"] if f["claim"] == "priya.raghavan@example.com"]
        assert emails, "expected the email to be extracted"
        assert emails[0]["evidence"][0]["locator"].startswith("line=")
        assert emails[0]["evidence"][0]["quote"]

    def test_duplicate_upload_is_409(self, client: TestClient, sample_tex_path: Path) -> None:
        candidate_id = _create(client)
        _upload(client, candidate_id, sample_tex_path)

        with sample_tex_path.open("rb") as handle:
            response = client.post(
                f"/candidates/{candidate_id}/resumes",
                files={"file": (sample_tex_path.name, handle, "application/x-tex")},
            )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "conflict"

    def test_unsupported_file_type_is_rejected(self, client: TestClient) -> None:
        candidate_id = _create(client)
        response = client.post(
            f"/candidates/{candidate_id}/resumes",
            files={"file": ("resume.exe", b"MZ\x90\x00", "application/octet-stream")},
        )
        assert response.status_code == 422
        assert "unsupported file type" in response.json()["error"]["message"]

    def test_empty_file_is_rejected(self, client: TestClient) -> None:
        candidate_id = _create(client)
        response = client.post(
            f"/candidates/{candidate_id}/resumes",
            files={"file": ("resume.txt", b"", "text/plain")},
        )
        assert response.status_code == 422

    def test_upload_to_unknown_candidate_is_404(
        self, client: TestClient, sample_tex_path: Path
    ) -> None:
        with sample_tex_path.open("rb") as handle:
            response = client.post(
                "/candidates/cand_missing/resumes",
                files={"file": (sample_tex_path.name, handle, "application/x-tex")},
            )
        assert response.status_code == 404


class TestFactReview:
    @pytest.fixture
    def candidate_with_facts(self, client: TestClient, sample_tex_path: Path) -> str:
        candidate_id = _create(client)
        _upload(client, candidate_id, sample_tex_path)
        return candidate_id

    def _first_fact(self, client: TestClient, candidate_id: str) -> dict[str, Any]:
        facts: list[dict[str, Any]] = client.get(f"/candidates/{candidate_id}").json()["facts"]
        return facts[0]

    def test_verify_marks_a_fact_confirmed(
        self, client: TestClient, candidate_with_facts: str
    ) -> None:
        fact = self._first_fact(client, candidate_with_facts)
        assert fact["verified"] is False

        response = client.post(f"/candidates/{candidate_with_facts}/facts/{fact['id']}/verify")
        assert response.status_code == 200
        assert response.json()["verified"] is True

    def test_correction_becomes_user_provided(
        self, client: TestClient, candidate_with_facts: str
    ) -> None:
        """A user's edit must not be presented as something the resume said."""
        fact = self._first_fact(client, candidate_with_facts)
        response = client.patch(
            f"/candidates/{candidate_with_facts}/facts/{fact['id']}",
            json={"claim": "Corrected by the candidate"},
        )
        assert response.status_code == 200

        body = response.json()
        assert body["claim"] == "Corrected by the candidate"
        assert body["provenance"] == Provenance.USER.value
        assert body["verified"] is True

    def test_empty_correction_is_rejected(
        self, client: TestClient, candidate_with_facts: str
    ) -> None:
        fact = self._first_fact(client, candidate_with_facts)
        response = client.patch(
            f"/candidates/{candidate_with_facts}/facts/{fact['id']}",
            json={"claim": "   "},
        )
        assert response.status_code == 422

    def test_user_added_fact_is_labelled_user_provided(
        self, client: TestClient, candidate_with_facts: str
    ) -> None:
        response = client.post(
            f"/candidates/{candidate_with_facts}/facts",
            json={
                "category": "work_authorization",
                "claim": "Authorised to work in India without sponsorship",
                "attributes": {"country": "IN"},
            },
        )
        assert response.status_code == 201

        body = response.json()
        assert body["provenance"] == Provenance.USER.value
        assert body["verified"] is True
        # User-asserted facts carry no resume evidence, and must not pretend to.
        assert body["evidence"] == []

    def test_duplicate_user_fact_is_409(
        self, client: TestClient, candidate_with_facts: str
    ) -> None:
        payload = {"category": "language", "claim": "Tamil (native)", "attributes": {}}
        assert (
            client.post(f"/candidates/{candidate_with_facts}/facts", json=payload).status_code
            == 201
        )
        assert (
            client.post(f"/candidates/{candidate_with_facts}/facts", json=payload).status_code
            == 409
        )

    def test_delete_removes_the_fact(self, client: TestClient, candidate_with_facts: str) -> None:
        fact = self._first_fact(client, candidate_with_facts)
        assert (
            client.delete(f"/candidates/{candidate_with_facts}/facts/{fact['id']}").status_code
            == 204
        )

        remaining = {
            item["id"] for item in client.get(f"/candidates/{candidate_with_facts}").json()["facts"]
        }
        assert fact["id"] not in remaining

    def test_fact_from_another_candidate_is_404(
        self, client: TestClient, candidate_with_facts: str
    ) -> None:
        """Cross-candidate access must fail even with a valid fact id."""
        other = _create(client, "Other Candidate")
        fact = self._first_fact(client, candidate_with_facts)
        response = client.post(f"/candidates/{other}/facts/{fact['id']}/verify")
        assert response.status_code == 404


class TestOpenApi:
    def test_candidate_routes_are_documented(self, client: TestClient) -> None:
        paths = client.get("/openapi.json").json()["paths"]
        assert "/candidates" in paths
        assert "/candidates/{candidate_id}/resumes" in paths
        assert "/candidates/{candidate_id}/facts/{fact_id}" in paths
