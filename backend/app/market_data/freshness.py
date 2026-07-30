"""Market data freshness (rule R-09).

A strategy cannot tell the difference between "the market did not move" and "we
stopped receiving data". This module makes that difference explicit, because
the two demand opposite responses: the first is normal, the second must stop new
entries.

The threshold is expressed in *intervals*, not seconds. Two hours behind is
routine on 4-hour candles and catastrophic on 15-minute ones, so a single
number of seconds would be wrong for one timeframe or the other.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.core.clock import Clock, SystemClock
from app.core.logging import get_logger
from app.domain.candle_series import is_stale, staleness
from app.domain.enums import HealthStatus, Timeframe
from app.monitoring.health import HealthCheck, HealthCheckResult

logger = get_logger(__name__)

#: Rule R-09, decided in Phase 3. One candle may legitimately still be forming;
#: two intervals of silence means the feed is behind and no entry may be taken
#: on it. Expressed as a multiple of the timeframe rather than in seconds.
DEFAULT_STALENESS_TOLERANCE_INTERVALS = 2


@dataclass(frozen=True, slots=True)
class FeedKey:
    """One symbol followed on one timeframe."""

    symbol: str
    timeframe: Timeframe

    def __str__(self) -> str:
        return f"{self.symbol}@{self.timeframe.value}"


@dataclass(frozen=True, slots=True)
class FeedFreshness:
    """What is known about one feed right now."""

    key: FeedKey
    last_candle_close_time: datetime | None
    age: timedelta | None
    is_stale: bool

    @property
    def has_data(self) -> bool:
        return self.last_candle_close_time is not None

    @property
    def status(self) -> HealthStatus:
        if not self.has_data:
            # Never HEALTHY: "no candle has arrived yet" is not "everything is
            # fine", it is "we do not know anything about this market".
            return HealthStatus.STARTING
        # DEGRADED rather than UNHEALTHY: R-09 rejects new orders, it does not
        # declare the service unusable. An open position still needs managing.
        return HealthStatus.DEGRADED if self.is_stale else HealthStatus.HEALTHY


class FeedFreshnessMonitor:
    """Tracks when each feed last delivered a closed candle.

    Deliberately in memory and deliberately not persisted: it answers "is the
    feed alive *now*", and a value that survived a restart would answer that
    question with data from before the restart.
    """

    def __init__(
        self,
        *,
        clock: Clock | None = None,
        tolerance_intervals: int = DEFAULT_STALENESS_TOLERANCE_INTERVALS,
    ) -> None:
        if tolerance_intervals < 1:
            raise ValueError("Tolerance must be at least one interval")
        self._clock = clock if clock is not None else SystemClock()
        self._tolerance = tolerance_intervals
        # The value is None for a feed that is required but has never delivered.
        self._last_close: dict[FeedKey, datetime | None] = {}

    def expect(self, symbol: str, timeframe: Timeframe) -> None:
        """Declare a feed as required before any candle has arrived.

        Without this, a feed that never connects would simply be absent from the
        report, and absence reads as "fine". Registering it up front turns a
        feed that never started into a visible STARTING entry.
        """
        self._last_close.setdefault(FeedKey(symbol.upper(), timeframe), None)

    def record_candle(self, symbol: str, timeframe: Timeframe, close_time: datetime) -> None:
        """Note that a closed candle arrived."""
        if close_time.tzinfo is None:
            raise ValueError("Candle close time must be timezone-aware")
        key = FeedKey(symbol.upper(), timeframe)
        previous = self._last_close.get(key)
        # Never moves backwards: a gap-fill delivering older candles must not
        # make the feed look fresher, and out-of-order arrival must not make it
        # look staler.
        if previous is None or close_time > previous:
            self._last_close[key] = close_time

    @property
    def keys(self) -> list[FeedKey]:
        return list(self._last_close)

    def freshness(self, key: FeedKey) -> FeedFreshness:
        last_close = self._last_close.get(key)
        if last_close is None:
            return FeedFreshness(key=key, last_candle_close_time=None, age=None, is_stale=True)
        now = self._clock.now()
        return FeedFreshness(
            key=key,
            last_candle_close_time=last_close,
            age=staleness(last_close, now),
            is_stale=is_stale(last_close, now, key.timeframe, self._tolerance),
        )

    def report(self) -> list[FeedFreshness]:
        return [self.freshness(key) for key in self._last_close]

    def status(self) -> HealthStatus:
        return HealthStatus.worst(entry.status for entry in self.report())


def _describe(entries: Iterable[FeedFreshness]) -> str:
    parts: list[str] = []
    for entry in entries:
        if entry.age is None:
            parts.append(f"{entry.key}=no data")
        else:
            parts.append(f"{entry.key}={int(entry.age.total_seconds())}s")
    return ", ".join(parts) if parts else "no feeds registered"


def build_market_data_check(monitor: FeedFreshnessMonitor) -> HealthCheck:
    """A health check reporting feed freshness (rule R-09)."""

    async def market_data_check() -> HealthCheckResult:
        started = time.perf_counter()
        entries = monitor.report()
        elapsed_ms = (time.perf_counter() - started) * 1000
        return HealthCheckResult(
            name="market_data",
            status=HealthStatus.worst(entry.status for entry in entries),
            duration_ms=round(elapsed_ms, 3),
            detail=_describe(entries),
        )

    return market_data_check
