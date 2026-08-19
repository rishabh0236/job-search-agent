"""API surface tests."""

from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from apps.api.main import create_app
from packages.core.errors import SafetyStop


class TestHealth:
    def test_reports_capabilities(self, client: TestClient) -> None:
        response = client.get("/health")
        assert response.status_code == 200

        body = response.json()
        assert body["app_env"] == "test"
        names = {capability["name"] for capability in body["capabilities"]}
        assert names == {"database", "llm", "latex"}

    def test_database_is_reachable(self, client: TestClient) -> None:
        capabilities = {c["name"]: c for c in client.get("/health").json()["capabilities"]}
        assert capabilities["database"]["status"] == "ok"

    def test_stub_provider_is_reported_as_degraded_not_ok(self, client: TestClient) -> None:
        """The UI must be able to tell that no live model is configured."""
        body = client.get("/health").json()
        llm = next(c for c in body["capabilities"] if c["name"] == "llm")
        assert llm["status"] == "degraded"
        assert body["status"] == "degraded"


class TestErrorMapping:
    """The exception handler is the contract the whole UI depends on.

    A safety stop must arrive as a structured 409 the frontend can render as an
    interrupt — not as a 500. Verified against a real route rather than by calling
    the handler directly, so middleware ordering is covered too.
    """

    def test_safety_stop_is_surfaced_with_a_reason_and_user_action_flag(self, db: Session) -> None:
        # serve_frontend=False: the SPA mount at / would shadow the route below.
        app = create_app(serve_frontend=False)

        @app.get("/_test/safety-stop")
        def _raise() -> None:
            raise SafetyStop(
                "A CAPTCHA appeared on the application form",
                reason="captcha",
                details={"url": "https://example.test/apply"},
            )

        with TestClient(app) as test_client:
            response = test_client.get("/_test/safety-stop")
        assert response.status_code == 409

        error = response.json()["error"]
        assert error["code"] == "safety_stop"
        assert error["reason"] == "captcha"
        assert error["requires_user_action"] is True
        assert "CAPTCHA" in error["message"]
