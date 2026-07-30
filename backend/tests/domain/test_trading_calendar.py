"""Trading calendar tests.

The daylight-saving cases are the reason this module exists. They are also the
cases a hand-rolled ``day_start + 24h`` gets wrong, twice a year, on days when
real positions are open.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.domain.trading_calendar import (
    day_end,
    day_length,
    day_start,
    is_in_no_entry_window,
    is_within_day,
    time_remaining_in_day,
    trading_date_for,
)

BUCHAREST = ZoneInfo("Europe/Bucharest")


class TestTradingDate:
    def test_an_instant_maps_to_its_local_date(self) -> None:
        # 21:30 UTC in July is 00:30 the NEXT day in Bucharest (UTC+3).
        instant = datetime(2026, 7, 28, 21, 30, tzinfo=UTC)
        assert trading_date_for(instant, BUCHAREST) == date(2026, 7, 29)

    def test_just_before_local_midnight_is_still_the_same_day(self) -> None:
        instant = datetime(2026, 7, 28, 20, 59, 59, tzinfo=UTC)
        assert trading_date_for(instant, BUCHAREST) == date(2026, 7, 28)

    def test_a_naive_datetime_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            trading_date_for(datetime(2026, 7, 28, 12, 0), BUCHAREST)  # noqa: DTZ001


class TestDayBoundaries:
    def test_summer_day_starts_at_21_utc_the_previous_day(self) -> None:
        """Bucharest is UTC+3 in summer."""
        assert day_start(date(2026, 7, 28), BUCHAREST) == datetime(2026, 7, 27, 21, 0, tzinfo=UTC)

    def test_winter_day_starts_at_22_utc_the_previous_day(self) -> None:
        """Bucharest is UTC+2 in winter."""
        assert day_start(date(2026, 1, 15), BUCHAREST) == datetime(2026, 1, 14, 22, 0, tzinfo=UTC)

    def test_the_end_of_one_day_is_the_start_of_the_next(self) -> None:
        """No gap and no overlap at the boundary."""
        assert day_end(date(2026, 7, 28), BUCHAREST) == day_start(date(2026, 7, 29), BUCHAREST)

    def test_an_ordinary_day_is_24_hours(self) -> None:
        assert day_length(date(2026, 7, 28), BUCHAREST) == timedelta(hours=24)


class TestDaylightSaving:
    """Twice a year a trading day is not 24 hours long."""

    def test_the_spring_day_is_23_hours(self) -> None:
        # Last Sunday of March: clocks jump 03:00 -> 04:00.
        assert day_length(date(2026, 3, 29), BUCHAREST) == timedelta(hours=23)

    def test_the_autumn_day_is_25_hours(self) -> None:
        # Last Sunday of October: clocks fall back 04:00 -> 03:00.
        assert day_length(date(2026, 10, 25), BUCHAREST) == timedelta(hours=25)

    def test_naive_arithmetic_would_be_wrong_on_those_days(self) -> None:
        """Why this module exists rather than day_start + 24h."""
        for shifting_day in (date(2026, 3, 29), date(2026, 10, 25)):
            naive_end = day_start(shifting_day, BUCHAREST) + timedelta(hours=24)
            assert naive_end != day_end(shifting_day, BUCHAREST)

    def test_boundaries_stay_contiguous_across_the_shift(self) -> None:
        """Even on the shifting days, no instant belongs to two days or none."""
        for shifting_day in (date(2026, 3, 29), date(2026, 10, 25)):
            assert day_end(shifting_day, BUCHAREST) == day_start(
                shifting_day + timedelta(days=1), BUCHAREST
            )


class TestMembership:
    def test_the_first_instant_belongs_to_the_day(self) -> None:
        trading_date = date(2026, 7, 28)
        assert is_within_day(day_start(trading_date, BUCHAREST), trading_date, BUCHAREST)

    def test_the_last_instant_belongs_to_the_next_day(self) -> None:
        """The upper bound is exclusive, which is what removes the ambiguity."""
        trading_date = date(2026, 7, 28)
        assert not is_within_day(day_end(trading_date, BUCHAREST), trading_date, BUCHAREST)
        assert is_within_day(
            day_end(trading_date, BUCHAREST), trading_date + timedelta(days=1), BUCHAREST
        )


class TestNoEntryWindow:
    """Rule R-24, Phase 0 decision OD-04."""

    def test_new_entries_are_blocked_in_the_last_30_minutes(self) -> None:
        trading_date = date(2026, 7, 28)
        instant = day_end(trading_date, BUCHAREST) - timedelta(minutes=20)
        assert is_in_no_entry_window(instant, trading_date, BUCHAREST, 30)

    def test_entries_are_allowed_before_the_window_opens(self) -> None:
        trading_date = date(2026, 7, 28)
        instant = day_end(trading_date, BUCHAREST) - timedelta(minutes=31)
        assert not is_in_no_entry_window(instant, trading_date, BUCHAREST, 30)

    def test_the_boundary_minute_is_blocked(self) -> None:
        """Exactly 30 minutes left counts as inside the window."""
        trading_date = date(2026, 7, 28)
        instant = day_end(trading_date, BUCHAREST) - timedelta(minutes=30)
        assert is_in_no_entry_window(instant, trading_date, BUCHAREST, 30)

    def test_a_window_of_zero_disables_the_rule(self) -> None:
        trading_date = date(2026, 7, 28)
        instant = day_end(trading_date, BUCHAREST) - timedelta(seconds=1)
        assert not is_in_no_entry_window(instant, trading_date, BUCHAREST, 0)

    def test_the_window_follows_the_real_day_end_on_a_shifting_day(self) -> None:
        """On the 25-hour day the window is still the last 30 real minutes."""
        trading_date = date(2026, 10, 25)
        inside = day_end(trading_date, BUCHAREST) - timedelta(minutes=10)
        outside = day_end(trading_date, BUCHAREST) - timedelta(hours=1)
        assert is_in_no_entry_window(inside, trading_date, BUCHAREST, 30)
        assert not is_in_no_entry_window(outside, trading_date, BUCHAREST, 30)

    def test_a_negative_window_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="cannot be negative"):
            is_in_no_entry_window(datetime.now(UTC), date(2026, 7, 28), BUCHAREST, -1)


class TestTimeRemaining:
    def test_remaining_time_never_goes_negative(self) -> None:
        trading_date = date(2026, 7, 28)
        after_the_day = day_end(trading_date, BUCHAREST) + timedelta(hours=5)
        assert time_remaining_in_day(after_the_day, trading_date, BUCHAREST) == timedelta(0)

    def test_remaining_time_at_the_start_is_the_whole_day(self) -> None:
        trading_date = date(2026, 10, 25)
        assert time_remaining_in_day(
            day_start(trading_date, BUCHAREST), trading_date, BUCHAREST
        ) == timedelta(hours=25)
