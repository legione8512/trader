"""Trading-day boundaries.

A trading day is a calendar day in a civil timezone (Europe/Bucharest), while
every timestamp is stored in UTC. Converting between the two is not a formatting
detail: get it wrong and every daily P&L figure is attributed to the wrong day.

**Daylight saving time matters.** In the European Union the clocks move on the
last Sunday of March and of October, so twice a year a trading day is 23 or 25
hours long, not 24. Code that assumes ``day_end = day_start + 24h`` is wrong on
exactly those two days - and those are days on which real positions are open.

Midnight itself is never ambiguous in Europe/Bucharest: the transition happens
at 03:00 local time, so ``00:00`` exists exactly once on every date. That is why
the day boundary can be computed directly, without disambiguation rules.

Pure module: no I/O, no clock reads. Every function takes the instant it needs.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


def trading_date_for(instant: datetime, timezone: ZoneInfo) -> date:
    """The civil date an instant belongs to.

    Requires a timezone-aware instant. A naive datetime here would silently be
    interpreted as local time on one machine and as UTC on another.
    """
    if instant.tzinfo is None:
        raise ValueError("trading_date_for requires a timezone-aware datetime")
    return instant.astimezone(timezone).date()


def day_start(trading_date: date, timezone: ZoneInfo) -> datetime:
    """First instant of the trading day, in UTC."""
    local_midnight = datetime.combine(trading_date, time.min, tzinfo=timezone)
    return local_midnight.astimezone(UTC)


def day_end(trading_date: date, timezone: ZoneInfo) -> datetime:
    """First instant of the NEXT trading day, in UTC.

    Exclusive upper bound, so ``day_start <= t < day_end`` covers the day with
    no gap and no overlap at the boundary.
    """
    return day_start(trading_date + timedelta(days=1), timezone)


def day_length(trading_date: date, timezone: ZoneInfo) -> timedelta:
    """How long the day actually is: 23, 24 or 25 hours."""
    return day_end(trading_date, timezone) - day_start(trading_date, timezone)


def is_within_day(instant: datetime, trading_date: date, timezone: ZoneInfo) -> bool:
    if instant.tzinfo is None:
        raise ValueError("is_within_day requires a timezone-aware datetime")
    moment = instant.astimezone(UTC)
    return day_start(trading_date, timezone) <= moment < day_end(trading_date, timezone)


def time_remaining_in_day(instant: datetime, trading_date: date, timezone: ZoneInfo) -> timedelta:
    """How much of the trading day is left. Never negative."""
    if instant.tzinfo is None:
        raise ValueError("time_remaining_in_day requires a timezone-aware datetime")
    remaining = day_end(trading_date, timezone) - instant.astimezone(UTC)
    return max(remaining, timedelta(0))


def is_in_no_entry_window(
    instant: datetime,
    trading_date: date,
    timezone: ZoneInfo,
    no_entry_minutes_before_end: int,
) -> bool:
    """Whether new entries are blocked because the day is about to end.

    Rule R-24. Phase 0 decision OD-04: open positions may cross the boundary,
    but a new position must not be opened on the closing day's risk budget
    minutes before that budget resets.

    A window of 0 disables the rule.
    """
    if no_entry_minutes_before_end < 0:
        raise ValueError("no_entry_minutes_before_end cannot be negative")
    if no_entry_minutes_before_end == 0:
        return False
    return time_remaining_in_day(instant, trading_date, timezone) <= timedelta(
        minutes=no_entry_minutes_before_end
    )
