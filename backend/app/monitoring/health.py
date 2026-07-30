"""Health check registry.

Checks are registered rather than hard-coded so that later phases can add
exchange connectivity and market-data freshness checks without touching the API
layer.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine

from app.config.settings import Settings
from app.domain.enums import HealthStatus
from app.persistence.database import ping


@dataclass(frozen=True, slots=True)
class HealthCheckResult:
    """Outcome of a single health check."""

    name: str
    status: HealthStatus
    duration_ms: float
    detail: str | None = None


HealthCheck = Callable[[], Awaitable[HealthCheckResult]]


class HealthRegistry:
    """Ordered collection of health checks."""

    def __init__(self) -> None:
        self._checks: dict[str, HealthCheck] = {}

    def register(self, name: str, check: HealthCheck, *, replace: bool = False) -> None:
        if name in self._checks and not replace:
            raise ValueError(f"Health check already registered: {name}")
        self._checks[name] = check

    @property
    def names(self) -> list[str]:
        return list(self._checks)

    async def run_all(self) -> list[HealthCheckResult]:
        """Run every check, isolating failures.

        A check that raises is reported as ``UNHEALTHY`` instead of propagating.
        A crashing health endpoint tells the operator nothing; a health endpoint
        that reports which check crashed tells them everything.
        """
        results: list[HealthCheckResult] = []
        for name, check in self._checks.items():
            started = time.perf_counter()
            try:
                results.append(await check())
            except Exception as exc:  # deliberate: one broken check must not
                # take down the whole health endpoint
                elapsed_ms = (time.perf_counter() - started) * 1000
                results.append(
                    HealthCheckResult(
                        name=name,
                        status=HealthStatus.UNHEALTHY,
                        duration_ms=round(elapsed_ms, 3),
                        # Only the exception TYPE, never its message. Driver
                        # errors routinely quote the full DSN, password
                        # included, and this value is returned over HTTP.
                        detail=f"check raised {type(exc).__name__}",
                    )
                )
        return results


def build_database_check(engine: AsyncEngine) -> HealthCheck:
    """Create a check that verifies the database answers a trivial query."""

    async def database_check() -> HealthCheckResult:
        started = time.perf_counter()
        try:
            await ping(engine)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - started) * 1000
            return HealthCheckResult(
                name="database",
                status=HealthStatus.UNHEALTHY,
                duration_ms=round(elapsed_ms, 3),
                # See the note in run_all: type only, never the message.
                detail=f"connection failed: {type(exc).__name__}",
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return HealthCheckResult(
            name="database",
            status=HealthStatus.HEALTHY,
            duration_ms=round(elapsed_ms, 3),
            detail="connection ok",
        )

    return database_check


def build_default_registry(
    settings: Settings,
    engine: AsyncEngine,
    *,
    extra_checks: Mapping[str, HealthCheck] | None = None,
) -> HealthRegistry:
    """Create the registry with the always-available checks.

    ``extra_checks`` is how Phase 3 adds market-data freshness and exchange
    connectivity without this module importing either: a check is a callable,
    and the registry does not care where it came from.
    """
    registry = HealthRegistry()

    async def application_check() -> HealthCheckResult:
        return HealthCheckResult(
            name="application",
            status=HealthStatus.HEALTHY,
            duration_ms=0.0,
            detail="process is running",
        )

    async def configuration_check() -> HealthCheckResult:
        started = time.perf_counter()
        detail = (
            f"mode={settings.autonomy_mode.value} "
            f"reference_capital={settings.bootstrap_reference_capital_ron} "
            f"{settings.reporting_currency} "
            f"quote={settings.exchange_quote_currency} "
            f"timezone={settings.trading_timezone}"
        )
        elapsed_ms = (time.perf_counter() - started) * 1000
        return HealthCheckResult(
            name="configuration",
            status=HealthStatus.HEALTHY,
            duration_ms=round(elapsed_ms, 3),
            detail=detail,
        )

    registry.register("application", application_check)
    registry.register("configuration", configuration_check)
    registry.register("database", build_database_check(engine))
    for name, check in (extra_checks or {}).items():
        registry.register(name, check)
    return registry
