"""Health endpoint.

Deliberately exposes no secret, no credential and no balance. It reports
operational state only, so that it stays safe to call from a container
orchestrator or an uptime monitor.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request, Response, status

from app import __version__
from app.api.base import ApiModel
from app.config.settings import Settings
from app.domain.enums import AutonomyMode, HealthStatus
from app.monitoring.health import HealthRegistry

router = APIRouter(tags=["monitoring"])


class HealthCheckPayload(ApiModel):
    """Result of one individual check."""

    name: str
    status: HealthStatus
    duration_ms: float
    detail: str | None = None


class HealthResponse(ApiModel):
    """Aggregated system health."""

    status: HealthStatus
    timestamp: datetime
    version: str
    environment: str
    autonomy_mode: AutonomyMode
    live_trading_enabled: bool
    checks: list[HealthCheckPayload]


def get_health_registry(request: Request) -> HealthRegistry:
    """Retrieve the registry built once at application startup."""
    registry: HealthRegistry = request.app.state.health_registry
    return registry


def get_app_settings(request: Request) -> Settings:
    """Return the settings this application instance was built with.

    Deliberately not the cached global ``get_settings()``: an application built
    by a test with custom settings must answer with those settings, not with
    whatever the process-wide cache happens to hold.
    """
    settings: Settings = request.app.state.settings
    return settings


@router.get(
    "/health",
    response_model=HealthResponse,
    summary="System health",
    responses={
        status.HTTP_200_OK: {"description": "System is healthy or degraded"},
        status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "System is unhealthy"},
    },
)
async def get_health(
    response: Response,
    settings: Settings = Depends(get_app_settings),
    registry: HealthRegistry = Depends(get_health_registry),
) -> HealthResponse:
    """Run every registered check and report the aggregated status."""
    results = await registry.run_all()
    overall = HealthStatus.worst(result.status for result in results)

    # DEGRADED still answers 200: the service is reachable and answering, it
    # simply must not open new positions. Only UNHEALTHY signals "do not use".
    if overall is HealthStatus.UNHEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return HealthResponse(
        status=overall,
        timestamp=datetime.now(UTC),
        version=__version__,
        environment=settings.app_env.value,
        autonomy_mode=settings.autonomy_mode,
        live_trading_enabled=settings.live_trading_enabled,
        checks=[
            HealthCheckPayload(
                name=result.name,
                status=result.status,
                duration_ms=result.duration_ms,
                detail=result.detail,
            )
            for result in results
        ],
    )
