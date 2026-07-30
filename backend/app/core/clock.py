"""Time source.

Reading the wall clock is I/O. Injecting it rather than calling
``datetime.now()`` deep inside a rule is what makes "block new entries in the
last 30 minutes of the day" testable without waiting until 23:30.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol


class Clock(Protocol):
    """Anything that can tell the current instant, always in UTC."""

    def now(self) -> datetime: ...


class SystemClock:
    """The real clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock frozen at one instant, for tests.

    Not a test-only convenience: replaying a decision during an incident
    investigation needs exactly this.
    """

    def __init__(self, instant: datetime) -> None:
        if instant.tzinfo is None:
            raise ValueError("FixedClock requires a timezone-aware instant")
        self._instant = instant.astimezone(UTC)

    def now(self) -> datetime:
        return self._instant

    def advance(self, seconds: float) -> None:
        from datetime import timedelta

        self._instant += timedelta(seconds=seconds)
