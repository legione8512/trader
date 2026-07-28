"""Core domain enumerations.

This module is part of the pure domain layer: it performs no I/O and imports
nothing from the infrastructure layers.
"""

from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum


class AppEnvironment(StrEnum):
    """Deployment environment the application is running in."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AutonomyMode(StrEnum):
    """How much authority the application has to act on its own decisions.

    The three modes are mutually exclusive. See docs/SRS.md section 7.
    """

    #: Analyse and propose. Never submits an exchange order.
    SIGNAL_ONLY = "SIGNAL_ONLY"

    #: Decide and execute automatically against the paper adapter only.
    PAPER_AUTOMATIC = "PAPER_AUTOMATIC"

    #: Real money on a real exchange. Disabled by default, four guards required.
    LIVE_AUTOMATIC = "LIVE_AUTOMATIC"


class HealthStatus(StrEnum):
    """System health state. See docs/STATE_MACHINES.md section 6.

    ``DEGRADED`` and ``UNHEALTHY`` both block opening new positions. Management
    of existing positions continues in every state: abandoning an open position
    is more dangerous than declining to open a new one.
    """

    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"

    @property
    def blocks_new_positions(self) -> bool:
        """Whether this state forbids opening new positions."""
        return self is not HealthStatus.HEALTHY

    @classmethod
    def worst(cls, statuses: Iterable[HealthStatus]) -> HealthStatus:
        """Aggregate several statuses into the most severe one.

        An empty input is treated as ``STARTING`` rather than ``HEALTHY``:
        "no checks ran" must never be reported as "everything is fine".
        """
        severity: dict[HealthStatus, int] = {
            cls.HEALTHY: 0,
            cls.STARTING: 1,
            cls.DEGRADED: 2,
            cls.UNHEALTHY: 3,
        }
        collected = list(statuses)
        if not collected:
            return cls.STARTING
        return max(collected, key=lambda status: severity[status])
