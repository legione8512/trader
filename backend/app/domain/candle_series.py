"""Reasoning about a series of candles.

Pure module: it takes candles and timestamps, and answers questions about them.
No I/O, no clock reads.

The questions are the ones that decide whether a strategy may run at all:

* is every candle aligned to its interval, or has something shifted;
* is the series contiguous, or is there a hole where the feed dropped;
* is the newest candle recent enough to act on.

A gap is not a cosmetic problem. An indicator computed across a hole is computed
on a period that never existed, and the resulting signal cannot be reproduced.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise

from app.domain.enums import Timeframe
from app.domain.errors import DomainError

#: Epoch used to test interval alignment. Exchange candle boundaries are
#: anchored to the Unix epoch in UTC, so a 15-minute candle opens at :00, :15,
#: :30 and :45 - never at :07.
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class CandleSeriesError(DomainError):
    """The series violates an invariant a strategy depends on."""


@dataclass(frozen=True, slots=True)
class CandleGap:
    """A stretch of missing candles, both bounds inclusive."""

    first_missing_open_time: datetime
    last_missing_open_time: datetime
    missing_count: int

    def __str__(self) -> str:
        return (
            f"{self.missing_count} missing from {self.first_missing_open_time.isoformat()} "
            f"to {self.last_missing_open_time.isoformat()}"
        )


def is_aligned(open_time: datetime, timeframe: Timeframe) -> bool:
    """Whether a candle opens on the interval grid.

    A misaligned candle means the feed, the parser or our assumption about the
    interval is wrong. Any of the three is a reason to stop, not to round.
    """
    if open_time.tzinfo is None:
        raise CandleSeriesError("Alignment needs a timezone-aware timestamp")
    offset = open_time.astimezone(UTC) - EPOCH
    return offset % timeframe.duration == timedelta(0)


def align_down(instant: datetime, timeframe: Timeframe) -> datetime:
    """The open time of the candle containing this instant."""
    if instant.tzinfo is None:
        raise CandleSeriesError("Alignment needs a timezone-aware timestamp")
    moment = instant.astimezone(UTC)
    offset = moment - EPOCH
    return EPOCH + (offset // timeframe.duration) * timeframe.duration


def expected_open_times(first: datetime, last: datetime, timeframe: Timeframe) -> list[datetime]:
    """Every open time the series should contain, both bounds inclusive."""
    if not is_aligned(first, timeframe) or not is_aligned(last, timeframe):
        raise CandleSeriesError("Range bounds must be aligned to the timeframe")
    if last < first:
        raise CandleSeriesError("Range end is before its start")

    times: list[datetime] = []
    cursor = first
    while cursor <= last:
        times.append(cursor)
        cursor += timeframe.duration
    return times


def validate_ordering(open_times: Sequence[datetime], timeframe: Timeframe) -> None:
    """Refuse a series that is unordered, duplicated or misaligned."""
    previous: datetime | None = None
    for open_time in open_times:
        if not is_aligned(open_time, timeframe):
            raise CandleSeriesError(
                f"Candle at {open_time.isoformat()} is not aligned to {timeframe.value}"
            )
        if previous is not None:
            if open_time == previous:
                raise CandleSeriesError(f"Duplicate candle at {open_time.isoformat()}")
            if open_time < previous:
                raise CandleSeriesError(f"Candles are out of order at {open_time.isoformat()}")
        previous = open_time
    return None


def find_gaps(open_times: Sequence[datetime], timeframe: Timeframe) -> list[CandleGap]:
    """Every hole in a series, as inclusive ranges of missing open times.

    The series must already be ordered and aligned; call ``validate_ordering``
    first. Consecutive missing candles are reported as one gap, because that is
    how a feed outage actually looks and one entry per missing candle would bury
    the signal.
    """
    if len(open_times) < 2:
        return []

    gaps: list[CandleGap] = []
    step = timeframe.duration
    for previous, current in pairwise(open_times):
        expected_next = previous + step
        if current == expected_next:
            continue
        missing_count = int((current - expected_next) / step)
        gaps.append(
            CandleGap(
                first_missing_open_time=expected_next,
                last_missing_open_time=current - step,
                missing_count=missing_count,
            )
        )
    return gaps


def is_contiguous(open_times: Sequence[datetime], timeframe: Timeframe) -> bool:
    return not find_gaps(open_times, timeframe)


def is_closed_at(close_time: datetime, now: datetime) -> bool:
    """Whether a candle had already closed at ``now``.

    The REST endpoint returns the IN-PROGRESS candle as the last element when
    the requested range reaches the present. That candle repaints until it
    closes, so persisting it would mean a backtest sees values that were not
    visible at decision time - look-ahead bias, introduced by an off-by-one.
    """
    if close_time.tzinfo is None or now.tzinfo is None:
        raise CandleSeriesError("Closure needs timezone-aware timestamps")
    return close_time < now


def staleness(latest_close_time: datetime, now: datetime) -> timedelta:
    """How long ago the newest candle closed. Never negative."""
    if latest_close_time.tzinfo is None or now.tzinfo is None:
        raise CandleSeriesError("Staleness needs timezone-aware timestamps")
    return max(now - latest_close_time, timedelta(0))


def is_stale(
    latest_close_time: datetime, now: datetime, timeframe: Timeframe, tolerance_factor: int = 2
) -> bool:
    """Whether the feed has fallen behind (rule R-09).

    The default tolerance is two intervals: one candle may legitimately still be
    forming, but two missed in a row means the feed is behind and no decision
    should be taken on it.
    """
    if tolerance_factor < 1:
        raise CandleSeriesError("Tolerance factor must be at least 1")
    return staleness(latest_close_time, now) > timeframe.duration * tolerance_factor
