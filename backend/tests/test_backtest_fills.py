"""The fill model.

Every test here corresponds to a way a backtest can flatter itself. They are
written as the refusal, not as the feature.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.backtest.fills import (
    Bar,
    ExitTrigger,
    FillAssumptions,
    FillModelError,
    simulate_exit,
    simulate_limit_entry,
)
from app.domain.enums import OrderSide

BASE = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def bar(index: int, *, open_: str, high: str, low: str, close: str) -> Bar:
    return Bar(
        open_time=BASE + timedelta(minutes=15 * index),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def flat(index: int, price: str) -> Bar:
    return bar(index, open_=price, high=price, low=price, close=price)


class TestEntryCannotFillOnTheSignalBar:
    def test_the_signal_bar_is_skipped_even_if_it_reaches_the_limit(self) -> None:
        """The decision is taken at that bar's close, so everything inside it
        had already happened. Filling there is look-ahead bias arriving through
        the execution model."""
        bars = [
            bar(0, open_="100", high="100", low="90", close="100"),  # touches 95
            flat(1, "100"),
        ]
        result = simulate_limit_entry(
            bars,
            signal_index=0,
            side=OrderSide.BUY,
            limit_price=Decimal("95"),
            timeout_bars=1,
        )
        assert result.filled is False

    def test_the_next_bar_can_fill(self) -> None:
        bars = [flat(0, "100"), bar(1, open_="100", high="100", low="94", close="99")]
        result = simulate_limit_entry(
            bars,
            signal_index=0,
            side=OrderSide.BUY,
            limit_price=Decimal("95"),
            timeout_bars=1,
        )
        assert result.filled is True
        assert result.bar_index == 1
        assert result.price == Decimal("95")


class TestEntryTimeout:
    def test_an_order_that_never_trades_expires(self) -> None:
        bars = [flat(index, "100") for index in range(5)]
        result = simulate_limit_entry(
            bars,
            signal_index=0,
            side=OrderSide.BUY,
            limit_price=Decimal("95"),
            timeout_bars=3,
        )
        assert result.filled is False

    def test_the_order_is_live_for_exactly_the_configured_bars(self) -> None:
        bars = [
            flat(0, "100"),
            flat(1, "100"),
            flat(2, "100"),
            bar(3, open_="100", high="100", low="94", close="99"),
        ]
        expired = simulate_limit_entry(
            bars,
            signal_index=0,
            side=OrderSide.BUY,
            limit_price=Decimal("95"),
            timeout_bars=2,
        )
        alive = simulate_limit_entry(
            bars,
            signal_index=0,
            side=OrderSide.BUY,
            limit_price=Decimal("95"),
            timeout_bars=3,
        )
        assert expired.filled is False
        assert alive.filled is True
        assert alive.bar_index == 3

    def test_a_zero_bar_timeout_is_refused(self) -> None:
        with pytest.raises(FillModelError, match="at least one bar"):
            simulate_limit_entry(
                [flat(0, "100")],
                signal_index=0,
                side=OrderSide.BUY,
                limit_price=Decimal("95"),
                timeout_bars=0,
            )


class TestEntryPrice:
    def test_a_bar_opening_through_the_limit_fills_at_the_open(self) -> None:
        """A real improvement, unlike the touch assumption. Refusing to model it
        would understate a genuine effect."""
        bars = [flat(0, "100"), bar(1, open_="93", high="96", low="92", close="95")]
        result = simulate_limit_entry(
            bars,
            signal_index=0,
            side=OrderSide.BUY,
            limit_price=Decimal("95"),
            timeout_bars=1,
        )
        assert result.price == Decimal("93")
        assert result.improved is True

    def test_a_touch_fills_at_the_limit_not_at_the_low(self) -> None:
        bars = [flat(0, "100"), bar(1, open_="100", high="101", low="90", close="99")]
        result = simulate_limit_entry(
            bars,
            signal_index=0,
            side=OrderSide.BUY,
            limit_price=Decimal("95"),
            timeout_bars=1,
        )
        assert result.price == Decimal("95")
        assert result.improved is False

    def test_a_short_entry_is_symmetric(self) -> None:
        bars = [flat(0, "100"), bar(1, open_="100", high="106", low="99", close="105")]
        result = simulate_limit_entry(
            bars,
            signal_index=0,
            side=OrderSide.SELL,
            limit_price=Decimal("105"),
            timeout_bars=1,
        )
        assert result.filled is True
        assert result.price == Decimal("105")


class TestTheAmbiguousBar:
    def test_a_bar_touching_both_levels_is_resolved_as_a_stop(self) -> None:
        """The single most important assumption in the module. Choosing the
        target is the fastest way to turn a losing strategy into a winning
        backtest."""
        bars = [bar(0, open_="100", high="110", low="90", close="105")]
        result = simulate_exit(
            bars,
            entry_index=0,
            side=OrderSide.BUY,
            stop_loss_price=Decimal("95"),
            take_profit_price=Decimal("108"),
        )
        assert result.trigger is ExitTrigger.STOP_LOSS
        assert result.ambiguous is True

    def test_an_unambiguous_target_is_still_a_target(self) -> None:
        """The stop assumption must not swallow the wins as well."""
        bars = [bar(0, open_="100", high="110", low="99", close="109")]
        result = simulate_exit(
            bars,
            entry_index=0,
            side=OrderSide.BUY,
            stop_loss_price=Decimal("95"),
            take_profit_price=Decimal("108"),
        )
        assert result.trigger is ExitTrigger.TAKE_PROFIT
        assert result.ambiguous is False


class TestGaps:
    def test_a_gap_through_the_stop_fills_at_the_open(self) -> None:
        """A loss larger than 1R is a real outcome. A model that caps every loss
        at exactly 1R describes a market that does not exist."""
        bars = [
            bar(0, open_="100", high="101", low="99", close="100"),
            bar(1, open_="88", high="89", low="85", close="86"),
        ]
        result = simulate_exit(
            bars,
            entry_index=0,
            side=OrderSide.BUY,
            stop_loss_price=Decimal("95"),
            take_profit_price=Decimal("110"),
        )
        assert result.trigger is ExitTrigger.STOP_LOSS
        assert result.price == Decimal("88")
        assert result.gapped is True

    def test_a_gap_through_the_target_fills_at_the_open(self) -> None:
        bars = [
            bar(0, open_="100", high="101", low="99", close="100"),
            bar(1, open_="115", high="118", low="114", close="117"),
        ]
        result = simulate_exit(
            bars,
            entry_index=0,
            side=OrderSide.BUY,
            stop_loss_price=Decimal("95"),
            take_profit_price=Decimal("110"),
        )
        assert result.price == Decimal("115")
        assert result.gapped is True

    def test_a_gap_below_the_stop_is_a_loss_bigger_than_one_r(self) -> None:
        entry = Decimal("100")
        stop = Decimal("95")
        bars = [
            bar(0, open_="100", high="101", low="99", close="100"),
            bar(1, open_="88", high="89", low="85", close="86"),
        ]
        result = simulate_exit(
            bars,
            entry_index=0,
            side=OrderSide.BUY,
            stop_loss_price=stop,
            take_profit_price=None,
        )
        realised = entry - result.price
        assert realised > (entry - stop)


class TestTheEntryBar:
    def test_the_exit_is_checked_from_the_entry_bar_itself(self) -> None:
        """The entry filled somewhere inside that bar. If the bar also reached
        the stop, we cannot know it did so beforehand - so we assume it did not
        save us."""
        bars = [bar(0, open_="100", high="101", low="90", close="99")]
        result = simulate_exit(
            bars,
            entry_index=0,
            side=OrderSide.BUY,
            stop_loss_price=Decimal("95"),
            take_profit_price=None,
        )
        assert result.trigger is ExitTrigger.STOP_LOSS
        assert result.bar_index == 0


class TestStillOpenAtTheEnd:
    def test_an_open_position_is_reported_not_discarded(self) -> None:
        """Dropping open positions at the end of a run removes whichever of them
        were losing."""
        bars = [flat(index, "100") for index in range(5)]
        result = simulate_exit(
            bars,
            entry_index=0,
            side=OrderSide.BUY,
            stop_loss_price=Decimal("95"),
            take_profit_price=Decimal("110"),
        )
        assert result.trigger is ExitTrigger.END_OF_DATA
        assert result.price == Decimal("100")
        assert result.bar_index == 4

    def test_a_maximum_holding_period_closes_the_position(self) -> None:
        bars = [flat(index, "100") for index in range(20)]
        result = simulate_exit(
            bars,
            entry_index=0,
            side=OrderSide.BUY,
            stop_loss_price=Decimal("95"),
            take_profit_price=Decimal("110"),
            max_bars=5,
        )
        assert result.trigger is ExitTrigger.END_OF_DATA
        assert result.bar_index == 4


class TestShortSide:
    def test_the_stop_is_above_and_the_target_below(self) -> None:
        bars = [bar(0, open_="100", high="106", low="99", close="105")]
        result = simulate_exit(
            bars,
            entry_index=0,
            side=OrderSide.SELL,
            stop_loss_price=Decimal("105"),
            take_profit_price=Decimal("90"),
        )
        assert result.trigger is ExitTrigger.STOP_LOSS

    def test_a_short_target_below_is_reached(self) -> None:
        bars = [bar(0, open_="100", high="101", low="89", close="90")]
        result = simulate_exit(
            bars,
            entry_index=0,
            side=OrderSide.SELL,
            stop_loss_price=Decimal("105"),
            take_profit_price=Decimal("90"),
        )
        assert result.trigger is ExitTrigger.TAKE_PROFIT


class TestBarValidation:
    def test_a_bar_with_a_high_below_its_low_is_refused(self) -> None:
        with pytest.raises(FillModelError, match="high below its low"):
            Bar(
                open_time=BASE,
                open=Decimal("100"),
                high=Decimal("90"),
                low=Decimal("110"),
                close=Decimal("100"),
            )


class TestAssumptionsAreCarried:
    def test_every_assumption_is_described_in_words(self) -> None:
        """A backtest read without its assumptions is a number without units."""
        described = FillAssumptions(entry_timeout_bars=4).describe()
        assert "optimistic" in described["fills_on_touch"]
        assert "stop is" in described["stop_wins_ambiguous_bars"]
        assert described["entry_allowed_on_signal_bar"].startswith("No")
        assert "exceed 1R" in described["gaps_modelled"]
