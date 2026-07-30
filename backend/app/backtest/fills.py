"""Simulating fills from candles.

This is where backtests lie, so every assumption is written down next to the
code that makes it. Four mistakes account for most inflated results, and each is
refused here explicitly:

**1. Filling on the signal candle.** The decision is taken AT that candle's
close, so the earliest an order can fill is the next candle. Filling on the
signal candle means trading on a price that had already happened when the
decision was made - the same look-ahead bias the indicators refuse, arriving
through the execution model instead.

**2. Assuming a limit order fills because the price touched it.** A touch means
someone traded there, not that our order was in the queue. This module still
fills on a touch, because candles carry no queue information - but it is an
ASSUMPTION, it is optimistic, and it is recorded on the result rather than
hidden in it.

**3. Deciding a bar that touched both the stop and the target in our favour.**
Without intrabar data nobody knows which came first. Assuming the target is the
single fastest way to turn a losing strategy into a winning backtest. **The stop
is always assumed to have filled first.**

**4. Ignoring gaps.** When a bar OPENS beyond the stop, the fill is at the open,
not at the stop. A loss larger than 1R is a real outcome, and a model that caps
every loss at exactly 1R is describing a market that does not exist.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from app.domain.enums import OrderSide
from app.domain.errors import DomainError

ZERO = Decimal(0)


class FillModelError(DomainError):
    """The simulation was given something it cannot interpret."""


class ExitTrigger(StrEnum):
    """Why a simulated position closed."""

    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    #: The run ended while the position was still open. Counted, never silently
    #: dropped: discarding open positions at the end of a backtest quietly
    #: removes whichever ones were losing.
    END_OF_DATA = "END_OF_DATA"
    #: Closed by a rule rather than by price - a day halt or a session close.
    FORCED = "FORCED"


@dataclass(frozen=True, slots=True)
class Bar:
    """One candle, reduced to what a fill simulation needs."""

    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise FillModelError(f"Bar at {self.open_time.isoformat()} has a high below its low")


@dataclass(frozen=True, slots=True)
class EntryFill:
    """Whether the entry order filled, and where."""

    filled: bool
    bar_index: int | None = None
    price: Decimal | None = None
    #: True when the bar opened through our limit and we got a better price.
    improved: bool = False


@dataclass(frozen=True, slots=True)
class ExitFill:
    """How the position ended."""

    trigger: ExitTrigger
    bar_index: int
    price: Decimal
    #: True when the bar gapped past the level and the fill was worse than it.
    gapped: bool = False
    #: True when the same bar touched both levels and the stop was assumed.
    ambiguous: bool = False


def simulate_limit_entry(
    bars: Sequence[Bar],
    *,
    signal_index: int,
    side: OrderSide,
    limit_price: Decimal,
    timeout_bars: int,
) -> EntryFill:
    """Try to fill a limit order placed at the close of ``signal_index``.

    Starts at ``signal_index + 1``. The order cannot fill on the bar that
    produced the signal: the decision was taken at that bar's close, and
    everything inside it had already happened.

    A bar that opens through the limit fills at the OPEN, not at the limit. That
    is a real improvement, unlike the touch assumption below it, and refusing to
    model it would understate a genuine effect.
    """
    if timeout_bars < 1:
        raise FillModelError("An entry order must be live for at least one bar")

    first = signal_index + 1
    last = min(first + timeout_bars - 1, len(bars) - 1)

    for index in range(first, last + 1):
        bar = bars[index]
        if side is OrderSide.BUY:
            if bar.open <= limit_price:
                return EntryFill(True, index, bar.open, improved=bar.open < limit_price)
            if bar.low <= limit_price:
                # A touch. Optimistic - it means somebody traded there, not that
                # we were in the queue - and recorded as an assumption.
                return EntryFill(True, index, limit_price)
        else:
            if bar.open >= limit_price:
                return EntryFill(True, index, bar.open, improved=bar.open > limit_price)
            if bar.high >= limit_price:
                return EntryFill(True, index, limit_price)

    return EntryFill(False)


def simulate_exit(
    bars: Sequence[Bar],
    *,
    entry_index: int,
    side: OrderSide,
    stop_loss_price: Decimal,
    take_profit_price: Decimal | None,
    max_bars: int | None = None,
) -> ExitFill:
    """Walk forward until the stop or the target is hit.

    Checking begins on the ENTRY bar itself. The entry filled somewhere inside
    that bar, and if the bar also reached the stop we cannot know it did so
    beforehand - so we assume it did not save us.
    """
    last = len(bars) - 1
    if max_bars is not None:
        last = min(last, entry_index + max_bars - 1)

    for index in range(entry_index, last + 1):
        bar = bars[index]
        hit_stop, stop_price, stop_gapped = _stop_hit(bar, side, stop_loss_price)
        hit_target, target_price, target_gapped = (
            _target_hit(bar, side, take_profit_price)
            if take_profit_price is not None
            else (False, ZERO, False)
        )

        if hit_stop and hit_target:
            # The assumption that keeps a backtest honest. Without intrabar data
            # nobody knows which came first, and choosing the target is the
            # fastest way to turn a losing strategy into a winning result.
            return ExitFill(
                ExitTrigger.STOP_LOSS, index, stop_price, gapped=stop_gapped, ambiguous=True
            )
        if hit_stop:
            return ExitFill(ExitTrigger.STOP_LOSS, index, stop_price, gapped=stop_gapped)
        if hit_target:
            return ExitFill(ExitTrigger.TAKE_PROFIT, index, target_price, gapped=target_gapped)

    # Still open. Marked and valued at the last close rather than discarded:
    # dropping open positions removes whichever of them were losing.
    return ExitFill(ExitTrigger.END_OF_DATA, last, bars[last].close)


def _stop_hit(bar: Bar, side: OrderSide, stop: Decimal) -> tuple[bool, Decimal, bool]:
    if side is OrderSide.BUY:
        if bar.open <= stop:
            # Gapped through. The fill is at the open, and the loss is larger
            # than 1R - which is a real outcome, not a modelling error.
            return True, bar.open, True
        if bar.low <= stop:
            return True, stop, False
    else:
        if bar.open >= stop:
            return True, bar.open, True
        if bar.high >= stop:
            return True, stop, False
    return False, ZERO, False


def _target_hit(bar: Bar, side: OrderSide, target: Decimal) -> tuple[bool, Decimal, bool]:
    if side is OrderSide.BUY:
        if bar.open >= target:
            return True, bar.open, True
        if bar.high >= target:
            return True, target, False
    else:
        if bar.open <= target:
            return True, bar.open, True
        if bar.low <= target:
            return True, target, False
    return False, ZERO, False


@dataclass(frozen=True, slots=True)
class FillAssumptions:
    """Everything the fill model assumes, carried on every result.

    Not a footnote. A backtest read without its assumptions is a number without
    units, and the touch-fill assumption in particular is optimistic enough to
    change a conclusion.
    """

    entry_timeout_bars: int
    fills_on_touch: bool = True
    stop_wins_ambiguous_bars: bool = True
    entry_allowed_on_signal_bar: bool = False

    def describe(self) -> dict[str, str]:
        return {
            "entry_timeout_bars": str(self.entry_timeout_bars),
            "fills_on_touch": (
                "A limit order is assumed to fill when price touches it. Candles "
                "carry no queue information, so this is optimistic."
            ),
            "stop_wins_ambiguous_bars": (
                "When one bar touches both the stop and the target, the stop is "
                "assumed to have filled first."
            ),
            "entry_allowed_on_signal_bar": (
                "No. The decision is taken at that bar's close, so the earliest "
                "fill is the following bar."
            ),
            "gaps_modelled": (
                "Yes. A bar opening beyond a level fills at the open, so a loss can exceed 1R."
            ),
        }
