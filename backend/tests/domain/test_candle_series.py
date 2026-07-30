"""Candle series tests. Pure domain, no exchange and no database."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.candle_series import (
    CandleSeriesError,
    align_down,
    expected_open_times,
    find_gaps,
    is_aligned,
    is_closed_at,
    is_contiguous,
    is_stale,
    staleness,
    validate_ordering,
)
from app.domain.enums import Timeframe

M15 = Timeframe.M15
BASE = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def series(*offsets_in_candles: int, timeframe: Timeframe = M15) -> list[datetime]:
    return [BASE + timeframe.duration * offset for offset in offsets_in_candles]


class TestAlignment:
    def test_a_15m_candle_opens_on_the_quarter_hour(self) -> None:
        for minute in (0, 15, 30, 45):
            assert is_aligned(BASE.replace(minute=minute), M15) is True

    def test_an_off_grid_open_time_is_not_aligned(self) -> None:
        """Misalignment means the feed, the parser or our assumption is wrong."""
        assert is_aligned(BASE.replace(minute=7), M15) is False

    def test_alignment_is_measured_from_the_unix_epoch(self) -> None:
        """Exchange boundaries are anchored to the epoch in UTC, not to today."""
        assert is_aligned(datetime(1970, 1, 1, 0, 15, tzinfo=UTC), M15) is True
        assert is_aligned(datetime(1970, 1, 1, 0, 14, tzinfo=UTC), M15) is False

    def test_align_down_finds_the_containing_candle(self) -> None:
        instant = datetime(2026, 7, 28, 12, 7, 33, tzinfo=UTC)
        assert align_down(instant, M15) == datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

    def test_align_down_leaves_an_already_aligned_time_alone(self) -> None:
        assert align_down(BASE, M15) == BASE

    def test_a_naive_timestamp_is_refused(self) -> None:
        with pytest.raises(CandleSeriesError, match="timezone-aware"):
            is_aligned(datetime(2026, 7, 28, 12, 0), M15)  # noqa: DTZ001

    def test_hourly_alignment_differs_from_quarter_hourly(self) -> None:
        quarter_past = datetime(2026, 7, 28, 12, 15, tzinfo=UTC)
        assert is_aligned(quarter_past, M15) is True
        assert is_aligned(quarter_past, Timeframe.H1) is False


class TestExpectedRange:
    def test_the_expected_series_includes_both_bounds(self) -> None:
        times = expected_open_times(BASE, BASE + M15.duration * 3, M15)
        assert times == series(0, 1, 2, 3)

    def test_a_single_candle_range_is_one_entry(self) -> None:
        assert expected_open_times(BASE, BASE, M15) == [BASE]

    def test_an_inverted_range_is_refused(self) -> None:
        with pytest.raises(CandleSeriesError, match="before its start"):
            expected_open_times(BASE, BASE - M15.duration, M15)

    def test_unaligned_bounds_are_refused(self) -> None:
        with pytest.raises(CandleSeriesError, match="aligned"):
            expected_open_times(BASE.replace(minute=7), BASE + M15.duration, M15)


class TestOrdering:
    def test_a_clean_series_passes(self) -> None:
        validate_ordering(series(0, 1, 2, 3), M15)

    def test_a_duplicate_candle_is_refused(self) -> None:
        with pytest.raises(CandleSeriesError, match="Duplicate"):
            validate_ordering(series(0, 1, 1, 2), M15)

    def test_an_out_of_order_series_is_refused(self) -> None:
        with pytest.raises(CandleSeriesError, match="out of order"):
            validate_ordering(series(0, 2, 1), M15)

    def test_a_misaligned_candle_is_refused(self) -> None:
        times = [BASE, BASE + timedelta(minutes=7)]
        with pytest.raises(CandleSeriesError, match="not aligned"):
            validate_ordering(times, M15)

    def test_an_empty_series_is_acceptable(self) -> None:
        validate_ordering([], M15)


class TestGapDetection:
    def test_a_contiguous_series_has_no_gaps(self) -> None:
        assert find_gaps(series(0, 1, 2, 3), M15) == []
        assert is_contiguous(series(0, 1, 2, 3), M15) is True

    def test_a_single_missing_candle_is_found(self) -> None:
        gaps = find_gaps(series(0, 1, 3, 4), M15)
        assert len(gaps) == 1
        assert gaps[0].missing_count == 1
        assert gaps[0].first_missing_open_time == BASE + M15.duration * 2
        assert gaps[0].last_missing_open_time == BASE + M15.duration * 2

    def test_consecutive_missing_candles_are_reported_as_one_gap(self) -> None:
        """That is how a feed outage actually looks; one entry per candle would
        bury the signal."""
        gaps = find_gaps(series(0, 5), M15)
        assert len(gaps) == 1
        assert gaps[0].missing_count == 4
        assert gaps[0].first_missing_open_time == BASE + M15.duration
        assert gaps[0].last_missing_open_time == BASE + M15.duration * 4

    def test_several_separate_outages_are_reported_separately(self) -> None:
        gaps = find_gaps(series(0, 2, 3, 6), M15)
        assert [gap.missing_count for gap in gaps] == [1, 2]

    def test_a_series_of_one_has_no_gaps(self) -> None:
        assert find_gaps(series(0), M15) == []

    def test_a_gap_describes_itself_readably(self) -> None:
        gap = find_gaps(series(0, 3), M15)[0]
        assert "2 missing" in str(gap)


class TestClosure:
    """The rule that keeps look-ahead bias out of the data."""

    def test_a_candle_that_closed_in_the_past_is_closed(self) -> None:
        close_time = BASE + M15.duration
        assert is_closed_at(close_time, close_time + timedelta(seconds=1)) is True

    def test_a_candle_closing_exactly_now_is_not_yet_closed(self) -> None:
        """Strictly in the past. At the boundary the candle can still change."""
        close_time = BASE + M15.duration
        assert is_closed_at(close_time, close_time) is False

    def test_the_in_progress_candle_is_not_closed(self) -> None:
        close_time = BASE + M15.duration
        assert is_closed_at(close_time, BASE + timedelta(minutes=3)) is False

    def test_naive_timestamps_are_refused(self) -> None:
        with pytest.raises(CandleSeriesError, match="timezone-aware"):
            is_closed_at(BASE, datetime(2026, 7, 28, 12, 0))  # noqa: DTZ001


class TestStaleness:
    """Rule R-09."""

    def test_staleness_is_never_negative(self) -> None:
        future = BASE + timedelta(hours=1)
        assert staleness(future, BASE) == timedelta(0)

    def test_one_interval_behind_is_not_yet_stale(self) -> None:
        """A candle may legitimately still be forming."""
        latest_close = BASE
        assert is_stale(latest_close, BASE + timedelta(minutes=14), M15) is False

    def test_more_than_two_intervals_behind_is_stale(self) -> None:
        latest_close = BASE
        assert is_stale(latest_close, BASE + timedelta(minutes=31), M15) is True

    def test_the_tolerance_is_configurable(self) -> None:
        latest_close = BASE
        thirty_one_minutes_later = BASE + timedelta(minutes=31)
        assert is_stale(latest_close, thirty_one_minutes_later, M15, tolerance_factor=3) is False

    def test_a_tolerance_below_one_is_refused(self) -> None:
        with pytest.raises(CandleSeriesError, match="at least 1"):
            is_stale(BASE, BASE, M15, tolerance_factor=0)

    def test_staleness_scales_with_the_timeframe(self) -> None:
        """Two hours behind is fine on 4h candles and disastrous on 15m ones."""
        two_hours_later = BASE + timedelta(hours=2)
        assert is_stale(BASE, two_hours_later, M15) is True
        assert is_stale(BASE, two_hours_later, Timeframe.H4) is False
