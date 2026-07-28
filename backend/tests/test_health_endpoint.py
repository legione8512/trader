"""Health endpoint tests."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import __version__
from app.config.settings import Settings
from app.domain.enums import AutonomyMode, HealthStatus
from app.main import create_app
from app.monitoring.health import HealthCheckResult, HealthRegistry


class TestHealthResponse:
    def test_returns_200_and_healthy(self, healthy_client: TestClient) -> None:
        response = healthy_client.get("/api/health")
        assert response.status_code == 200
        assert response.json()["status"] == HealthStatus.HEALTHY.value

    def test_payload_uses_camel_case(self, healthy_client: TestClient) -> None:
        """The frontend consumes camelCase; Python stays snake_case."""
        payload = response_json(healthy_client)
        assert "autonomyMode" in payload
        assert "liveTradingEnabled" in payload
        assert "durationMs" in payload["checks"][0]
        assert "autonomy_mode" not in payload

    def test_reports_operational_state(self, healthy_client: TestClient) -> None:
        payload = response_json(healthy_client)
        assert payload["version"] == __version__
        assert payload["environment"] == "test"
        assert payload["autonomyMode"] == AutonomyMode.SIGNAL_ONLY.value
        assert payload["liveTradingEnabled"] is False

    def test_runs_every_registered_check(self, healthy_client: TestClient) -> None:
        names = {check["name"] for check in response_json(healthy_client)["checks"]}
        assert names == {"application", "configuration", "database"}

    def test_timestamp_is_timezone_aware_utc(self, healthy_client: TestClient) -> None:
        timestamp = response_json(healthy_client)["timestamp"]
        assert timestamp.endswith("Z") or "+00:00" in timestamp


class TestDatabaseDown:
    """The default test application points at an unreachable database."""

    def test_unreachable_database_makes_the_system_unhealthy(self, client: TestClient) -> None:
        response = client.get("/api/health")
        assert response.status_code == 503
        assert response.json()["status"] == HealthStatus.UNHEALTHY.value

    def test_other_checks_still_report_healthy(self, client: TestClient) -> None:
        payload = client.get("/api/health").json()
        healthy = {c["name"] for c in payload["checks"] if c["status"] == "HEALTHY"}
        assert healthy == {"application", "configuration"}

    def test_database_failure_detail_names_no_credentials(self, client: TestClient) -> None:
        """Driver errors quote the whole DSN. The detail must be type-only."""
        payload = client.get("/api/health").json()
        database = next(c for c in payload["checks"] if c["name"] == "database")
        assert database["status"] == HealthStatus.UNHEALTHY.value
        assert database["detail"].startswith("connection failed: ")
        assert "unit-test-password" not in database["detail"]
        assert "127.0.0.1" not in database["detail"]


class TestFailureIsolation:
    def test_a_crashing_check_becomes_unhealthy_and_503(self, settings: Settings) -> None:
        """A health endpoint that crashes tells the operator nothing."""
        app = create_app(settings)

        async def exploding_check() -> HealthCheckResult:
            raise RuntimeError("secret-bearing driver message")

        registry: HealthRegistry = app.state.health_registry
        registry.register("exchange", exploding_check)

        with TestClient(app) as client:
            response = client.get("/api/health")

        assert response.status_code == 503
        payload = response.json()
        assert payload["status"] == HealthStatus.UNHEALTHY.value

        exchange = next(c for c in payload["checks"] if c["name"] == "exchange")
        assert exchange["detail"] == "check raised RuntimeError"
        # The exception MESSAGE must never reach the response body.
        assert "secret-bearing driver message" not in response.text


class TestNoSecretLeak:
    def test_response_contains_no_secret(self, make_settings: Callable[..., Settings]) -> None:
        """AC-14: secrets must never appear in an API response."""
        secret = "paper-secret-abcdefghijklmnopqrstuvwxyz"
        settings = make_settings(
            binance_paper_api_key="paper-key-abcdefghijklmnopqrstuvwxyz",
            binance_paper_api_secret=secret,
        )
        app: FastAPI = create_app(settings)

        with TestClient(app) as client:
            body = client.get("/api/health").text

        assert secret not in body
        assert "paper-key-abcdefghijklmnopqrstuvwxyz" not in body

    def test_response_contains_no_database_password(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(
            database_url="postgresql+asyncpg://trader:supersecretpassword@127.0.0.1:1/trader"
        )
        app = create_app(settings)

        with TestClient(app) as client:
            body = client.get("/api/health").text

        assert "supersecretpassword" not in body


def response_json(client: TestClient) -> dict:  # type: ignore[type-arg]
    response = client.get("/api/health")
    assert response.status_code == 200
    payload: dict = response.json()  # type: ignore[type-arg]
    return payload
