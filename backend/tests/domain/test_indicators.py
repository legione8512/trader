"""Indicator tests.

Two kinds of test here. The ordinary ones check known values. The one that
matters most is ``TestNoLookAhead``: it asserts the property that makes a
backtest meaningful, over every indicator at once.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from app.domain.indicators import (
    IndicatorError,
    IndicatorSeries,
    average_true_range,
    exponential_moving_average,
    is_warm,
    latest,
    relative_strength_index,
    rolling_maximum,
    rolling_minimum,
    simple_moving_average,
    true_range,
    warmup_length,
    wilder_moving_average,
)


def decimals(*values: str | int) -> list[Decimal]:
    return [Decimal(str(value)) for value in values]


#: A deliberately irregular series: rising, falling, flat and gapping, so an
#: indicator cannot look right by accident on a monotonic ramp.
CLOSES = decimals(
    100, 102, 101, 105, 107, 106, 110, 109, 112, 115, 113, 118, 117, 120, 119, 125, 124, 130
)
HIGHS = [value + Decimal(2) for value in CLOSES]
LOWS = [value - Decimal(2) for value in CLOSES]


class TestSimpleMovingAverage:
    def test_the_mean_is_exact(self) -> None:
        result = simple_moving_average(decimals(1, 2, 3, 4), 2)
        assert result == [None, Decimal("1.5"), Decimal("2.5"), Decimal("3.5")]

    def test_warm_up_is_period_minus_one(self) -> None:
        result = simple_moving_average(CLOSES, 5)
        assert warmup_length(result) == 4

    def test_a_series_shorter_than_the_period_produces_nothing(self) -> None:
        """Not an error and not a partial average: there is simply no value
        yet, and a partial one would be a different indicator."""
        result = simple_moving_average(decimals(1, 2), 5)
        assert result == [None, None]

    def test_the_rolling_update_matches_a_full_recomputation(self) -> None:
        """The rolling sum is an optimisation; it must not change the answer."""
        period = 4
        rolled = simple_moving_average(CLOSES, period)
        for index in range(period - 1, len(CLOSES)):
            window = CLOSES[index - period + 1 : index + 1]
            assert rolled[index] == sum(window) / Decimal(period)

    def test_a_period_below_one_is_refused(self) -> None:
        with pytest.raises(IndicatorError, match="at least 1"):
            simple_moving_average(CLOSES, 0)

    def test_an_empty_series_gives_an_empty_result(self) -> None:
        assert simple_moving_average([], 3) == []


class TestExponentialMovingAverage:
    def test_it_is_seeded_with_the_simple_average(self) -> None:
        """Stated explicitly because seeding with the first value instead gives
        a different series for hundreds of bars."""
        values = decimals(1, 2, 3, 4, 5)
        result = exponential_moving_average(values, 3)
        assert result[2] == Decimal(2)

    def test_it_uses_the_standard_smoothing_factor(self) -> None:
        values = decimals(1, 2, 3, 4)
        result = exponential_moving_average(values, 3)
        # multiplier = 2/(3+1) = 0.5; seed 2 then (4 - 2) * 0.5 + 2 = 3
        assert result[3] == Decimal(3)

    def test_it_reacts_faster_than_the_simple_average(self) -> None:
        rising = decimals(10, 10, 10, 10, 10, 20)
        exponential = exponential_moving_average(rising, 5)
        simple = simple_moving_average(rising, 5)
        assert exponential[-1] is not None and simple[-1] is not None
        assert exponential[-1] > simple[-1]

    def test_a_flat_series_stays_flat(self) -> None:
        flat = decimals(7, 7, 7, 7, 7, 7)
        assert exponential_moving_average(flat, 3)[-1] == Decimal(7)


class TestWilderMovingAverage:
    def test_it_smooths_more_slowly_than_an_equal_period_ema(self) -> None:
        """Wilder(14) behaves like EMA(27). An ATR computed with a plain EMA is
        a different, faster indicator wearing the same name."""
        rising = decimals(10, 10, 10, 10, 10, 20)
        wilder = wilder_moving_average(rising, 5)
        exponential = exponential_moving_average(rising, 5)
        assert wilder[-1] is not None and exponential[-1] is not None
        assert wilder[-1] < exponential[-1]

    def test_the_recurrence_is_wilders(self) -> None:
        values = decimals(1, 1, 1, 1, 5)
        result = wilder_moving_average(values, 4)
        # seed 1, then 1 + (5 - 1)/4 = 2
        assert result[4] == Decimal(2)


class TestTrueRange:
    def test_the_first_observation_has_no_true_range(self) -> None:
        """Without a previous close it is the bar range, which ignores exactly
        the gap the indicator exists to capture."""
        result = true_range(HIGHS, LOWS, CLOSES)
        assert result[0] is None

    def test_a_gap_up_widens_the_range_beyond_the_bar(self) -> None:
        highs = decimals(10, 30)
        lows = decimals(8, 28)
        closes = decimals(9, 29)
        result = true_range(highs, lows, closes)
        # bar range is 2, but the gap from the previous close of 9 makes it 21
        assert result[1] == Decimal(21)

    def test_series_of_different_lengths_are_refused(self) -> None:
        with pytest.raises(IndicatorError, match="equal length"):
            true_range(decimals(1, 2), decimals(1), decimals(1, 2))


class TestAverageTrueRange:
    def test_warm_up_costs_one_more_than_the_period(self) -> None:
        """ATR(14) needs 15 candles: the first true range is undefined."""
        result = average_true_range(HIGHS, LOWS, CLOSES, 5)
        assert warmup_length(result) == 5
        assert result[4] is None
        assert result[5] is not None

    def test_it_is_aligned_to_the_input(self) -> None:
        result = average_true_range(HIGHS, LOWS, CLOSES, 5)
        assert len(result) == len(CLOSES)

    def test_a_constant_range_gives_that_range(self) -> None:
        highs = [Decimal(12)] * 8
        lows = [Decimal(10)] * 8
        closes = [Decimal(11)] * 8
        assert average_true_range(highs, lows, closes, 3)[-1] == Decimal(2)

    def test_it_is_always_positive_for_real_bars(self) -> None:
        for value in average_true_range(HIGHS, LOWS, CLOSES, 5):
            if value is not None:
                assert value > 0

    def test_an_empty_series_gives_an_empty_result(self) -> None:
        assert average_true_range([], [], [], 3) == []

    def test_a_single_candle_produces_no_value(self) -> None:
        result = average_true_range(decimals(10), decimals(8), decimals(9), 3)
        assert result == [None]


class TestRelativeStrengthIndex:
    def test_warm_up_is_the_period(self) -> None:
        result = relative_strength_index(CLOSES, 5)
        assert warmup_length(result) == 5

    def test_a_series_that_only_rises_reaches_one_hundred(self) -> None:
        rising = decimals(1, 2, 3, 4, 5, 6, 7, 8)
        assert relative_strength_index(rising, 3)[-1] == Decimal(100)

    def test_a_series_that_only_falls_reaches_zero(self) -> None:
        falling = decimals(8, 7, 6, 5, 4, 3, 2, 1)
        assert relative_strength_index(falling, 3)[-1] == Decimal(0)

    def test_a_flat_series_reads_neutral(self) -> None:
        """No gains and no losses is no directional pressure, not an error and
        not maximum strength."""
        flat = decimals(5, 5, 5, 5, 5, 5)
        assert relative_strength_index(flat, 3)[-1] == Decimal(50)

    def test_it_stays_within_its_bounds(self) -> None:
        for value in relative_strength_index(CLOSES, 5):
            if value is not None:
                assert Decimal(0) <= value <= Decimal(100)

    def test_a_series_shorter_than_the_period_produces_nothing(self) -> None:
        assert relative_strength_index(decimals(1, 2, 3), 5) == [None, None, None]


class TestRollingExtremes:
    def test_the_window_includes_the_current_observation(self) -> None:
        values = decimals(1, 5, 3)
        assert rolling_maximum(values, 2) == [None, Decimal(5), Decimal(5)]
        assert rolling_minimum(values, 2) == [None, Decimal(1), Decimal(3)]

    def test_an_old_extreme_leaves_the_window(self) -> None:
        """Otherwise a breakout level would never reset."""
        values = decimals(10, 1, 2, 3)
        assert rolling_maximum(values, 2)[-1] == Decimal(3)


class TestHelpers:
    def test_warmup_length_counts_the_leading_gap(self) -> None:
        assert warmup_length([None, None, Decimal(1)]) == 2

    def test_a_series_that_never_warms_reports_its_full_length(self) -> None:
        assert warmup_length([None, None]) == 2

    def test_is_warm_reflects_the_last_value(self) -> None:
        assert is_warm([None, Decimal(1)]) is True
        assert is_warm([Decimal(1), None]) is False
        assert is_warm([]) is False

    def test_latest_returns_the_final_value(self) -> None:
        assert latest([Decimal(1), Decimal(2)]) == Decimal(2)
        assert latest([]) is None


#: Every indicator, reduced to one signature so the look-ahead property can be
#: asserted over all of them rather than remembered for each.
INDICATORS: dict[str, Callable[[int], IndicatorSeries]] = {
    "sma": lambda n: simple_moving_average(CLOSES[:n], 5),
    "ema": lambda n: exponential_moving_average(CLOSES[:n], 5),
    "wilder": lambda n: wilder_moving_average(CLOSES[:n], 5),
    "true_range": lambda n: true_range(HIGHS[:n], LOWS[:n], CLOSES[:n]),
    "atr": lambda n: average_true_range(HIGHS[:n], LOWS[:n], CLOSES[:n], 5),
    "rsi": lambda n: relative_strength_index(CLOSES[:n], 5),
    "rolling_max": lambda n: rolling_maximum(CLOSES[:n], 5),
    "rolling_min": lambda n: rolling_minimum(CLOSES[:n], 5),
}


class TestNoLookAhead:
    """The property the whole module exists to guarantee.

    ``result[i]`` must depend only on observations up to ``i``. If it did not,
    a backtest would compute indicator values using candles that had not
    happened yet, and every result it produced would be fiction.
    """

    @pytest.mark.parametrize("name", sorted(INDICATORS))
    def test_truncating_the_future_changes_nothing_in_the_past(self, name: str) -> None:
        compute = INDICATORS[name]
        full = compute(len(CLOSES))

        for cut in range(1, len(CLOSES) + 1):
            partial = compute(cut)
            assert len(partial) == cut, f"{name} is not aligned to its input at length {cut}"
            assert partial == full[:cut], (
                f"{name} at length {cut} differs from the full series. "
                f"The indicator is reading the future."
            )

    @pytest.mark.parametrize("name", sorted(INDICATORS))
    def test_every_indicator_is_aligned_to_its_input(self, name: str) -> None:
        assert len(INDICATORS[name](len(CLOSES))) == len(CLOSES)

    @pytest.mark.parametrize("name", sorted(INDICATORS))
    def test_no_indicator_returns_a_float(self, name: str) -> None:
        """AC-17. A float here would silently discard exactness and make the
        backtest irreproducible across machines."""
        for value in INDICATORS[name](len(CLOSES)):
            assert value is None or isinstance(value, Decimal)
