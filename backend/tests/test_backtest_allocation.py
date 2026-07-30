"""Allocation rules.

The tests that matter are the ones that would catch a rule getting credit for
information it did not have.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.backtest.allocation import (
    AllocationError,
    AlwaysInvested,
    MovingAverageRegime,
    simulate_allocation,
)
from app.domain.candle_window import CandleWindow
from app.domain.enums import Timeframe

D1 = Timeframe.D1
BASE = datetime(2026, 1, 1, tzinfo=UTC)
NO_FEE = Decimal(0)
FEE = Decimal("0.00075")


def window(closes: Sequence[Decimal]) -> CandleWindow:
    return CandleWindow(
        symbol="BTCUSDT",
        timeframe=D1,
        open_times=tuple(BASE + D1.duration * index for index in range(len(closes))),
        opens=tuple(closes),
        highs=tuple(close * Decimal("1.01") for close in closes),
        lows=tuple(close * Decimal("0.99") for close in closes),
        closes=tuple(closes),
        volumes=tuple(Decimal(1) for _ in closes),
    )


def rising(count: int, rate: str = "1.01") -> list[Decimal]:
    closes = [Decimal(100)]
    for _ in range(count - 1):
        closes.append(closes[-1] * Decimal(rate))
    return closes


class TestNoLookAhead:
    def test_a_rule_cannot_earn_the_bar_it_decided_on(self) -> None:
        """The single easiest way to make an allocation rule look
        extraordinary: decide at the close of bar i, then collect bar i's move.

        Here the price jumps on the final bar. A rule that saw it would capture
        the jump; one that decides from the close before it cannot.
        """
        closes = [Decimal(100)] * 10 + [Decimal(200)]

        class SeesTheJump:
            name = "cheat"
            warmup_bars = 1

            def signals(self, w: CandleWindow) -> list[bool | None]:
                # True only on the bar whose CLOSE is already 200 - so acting on
                # it can only buy at 200, never capture the move to it.
                return [close >= Decimal(200) for close in w.closes]

        result = simulate_allocation(window(closes), SeesTheJump(), fee_per_side=NO_FEE)
        assert result.total_return_percent == Decimal("0.00")

    def test_a_rule_that_is_right_one_bar_early_does_capture_the_move(self) -> None:
        """The mirror of the test above: the mechanism works, it is only the
        timing that is enforced."""
        closes = [Decimal(100)] * 10 + [Decimal(200)]

        class OneBarEarly:
            name = "early"
            warmup_bars = 1

            def signals(self, w: CandleWindow) -> list[bool | None]:
                return [index == len(w.closes) - 2 for index in range(len(w.closes))]

        result = simulate_allocation(window(closes), OneBarEarly(), fee_per_side=NO_FEE)
        assert result.total_return_percent == Decimal("100.00")


class TestBenchmark:
    def test_always_invested_equals_the_price_move(self) -> None:
        closes = rising(50)
        result = simulate_allocation(window(closes), AlwaysInvested(), fee_per_side=NO_FEE)
        expected = (closes[-1] / closes[0] - 1) * 100
        assert abs(result.total_return_percent - expected) < Decimal("0.01")

    def test_the_benchmark_runs_through_the_same_simulation(self) -> None:
        """A benchmark computed by a different code path is one that can
        disagree with the thing it benchmarks."""
        result = simulate_allocation(window(rising(50)), AlwaysInvested(), fee_per_side=FEE)
        assert result.switches == 1
        assert result.time_invested_percent == Decimal("100.0")


class TestFees:
    def test_every_switch_costs(self) -> None:
        closes = [Decimal(100), Decimal(110), Decimal(90), Decimal(110), Decimal(90)] * 6

        class Flipper:
            name = "flip"
            warmup_bars = 1

            def signals(self, w: CandleWindow) -> list[bool | None]:
                return [index % 2 == 0 for index in range(len(w.closes))]

        free = simulate_allocation(window(closes), Flipper(), fee_per_side=NO_FEE)
        charged = simulate_allocation(window(closes), Flipper(), fee_per_side=FEE)
        assert charged.switches > 10
        assert charged.total_return_percent < free.total_return_percent
        assert charged.fees_paid_percent > 0

    def test_a_rule_that_never_switches_pays_once(self) -> None:
        result = simulate_allocation(window(rising(50)), AlwaysInvested(), fee_per_side=FEE)
        assert result.fees_paid_percent == Decimal("0.08")


class TestMovingAverageRegime:
    def test_it_holds_while_price_leads_the_average(self) -> None:
        result = simulate_allocation(
            window(rising(120)), MovingAverageRegime(period=20), fee_per_side=NO_FEE
        )
        assert result.time_invested_percent == Decimal("100.0")

    def test_it_stays_out_of_a_falling_market(self) -> None:
        falling = [Decimal(100) * (Decimal("0.99") ** index) for index in range(120)]
        result = simulate_allocation(
            window(falling), MovingAverageRegime(period=20), fee_per_side=NO_FEE
        )
        assert result.time_invested_percent == Decimal("0.0")
        assert result.total_return_percent == Decimal("0.00")

    def test_a_period_below_two_is_refused(self) -> None:
        with pytest.raises(AllocationError, match="at least 2"):
            MovingAverageRegime(period=1)

    def test_a_window_shorter_than_the_warm_up_is_refused(self) -> None:
        with pytest.raises(AllocationError, match="more than"):
            simulate_allocation(
                window(rising(30)), MovingAverageRegime(period=100), fee_per_side=NO_FEE
            )


class TestComparison:
    def test_beating_the_benchmark_requires_both_measures(self) -> None:
        """A rule that earns more while risking more has improved nothing. A
        larger position does that too, and needs no rule."""
        falling_then_rising = [Decimal(100) * (Decimal("0.98") ** index) for index in range(60)] + [
            Decimal(30) * (Decimal("1.03") ** index) for index in range(120)
        ]
        chart = window(falling_then_rising)
        benchmark = simulate_allocation(chart, AlwaysInvested(), fee_per_side=FEE, start_at=50)
        regime = simulate_allocation(
            chart, MovingAverageRegime(period=50), fee_per_side=FEE, start_at=50
        )

        better, explanation = regime.beats(benchmark)
        assert isinstance(better, bool)
        assert regime.name in explanation

    def test_a_rule_that_only_reduces_risk_is_not_called_a_win(self) -> None:
        closes = rising(200)
        chart = window(closes)
        benchmark = simulate_allocation(chart, AlwaysInvested(), fee_per_side=FEE, start_at=100)
        regime = simulate_allocation(
            chart, MovingAverageRegime(period=100), fee_per_side=FEE, start_at=100
        )
        better, explanation = regime.beats(benchmark)
        assert better is False
        assert "decision, not a result" in explanation or "worse" in explanation

    def test_every_rule_is_scored_over_the_same_bars(self) -> None:
        """Otherwise the difference between two rules is partly the difference
        between two stretches of market."""
        chart = window(rising(300))
        fast = simulate_allocation(
            chart, MovingAverageRegime(period=50), fee_per_side=NO_FEE, start_at=200
        )
        slow = simulate_allocation(
            chart, MovingAverageRegime(period=200), fee_per_side=NO_FEE, start_at=200
        )
        assert fast.bars == slow.bars
