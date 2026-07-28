"""Health check registry.

Checks are registered rather than hard-coded so that later phases can add a
database check, an exchange connectivity check and a market-data freshness
check without touching the API layer.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.config.settings import Settings
from app.domain.enums import HealthStatus


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

    def register(self, name: str, check: HealthCheck) -> None:
        if name in self._checks:
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
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
        return results


def build_default_registry(settings: Settings) -> HealthRegistry:
    """Create the registry with the checks available in Phase 1.

    The database check arrives in milestone 1.3, the exchange and market-data
    checks in Phase 3.
    """
    registry = HealthRegistry()

    async def application_check() -> HealthCheckResult:
        started = time.perf_counter()
        elapsed_ms = (time.perf_counter() - started) * 1000
        return HealthCheckResult(
            name="application",
            status=HealthStatus.HEALTHY,
            duration_ms=round(elapsed_ms, 3),
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
    return registry
