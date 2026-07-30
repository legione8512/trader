"""Allocation rules: be in the asset, or be in cash.

A regime filter is not a strategy that places trades against a stop. It answers
one question per bar - hold or do not hold - and everything about position
sizing, stop distance and reward-to-risk is beside the point. Forcing it through
the trade engine would measure the wrong thing precisely.

So it gets its own model, with the same discipline:

**The position held during bar i+1 is decided from the close of bar i.** Earning
bar i's return from a decision made at bar i's close is the single easiest way
to make an allocation rule look extraordinary, and it is the mistake this module
exists to make impossible.

**Every switch pays a fee**, on one side. A rule that flips daily is not free
just because it holds a real asset in between.

**The benchmark is buy-and-hold over the identical window.** Not zero. A rule
that keeps you in a rising asset 60% of the time and calls the result a success
has measured itself against nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Protocol

from app.domain.candle_window import CandleWindow
from app.domain.errors import DomainError
from app.domain.indicators import IndicatorSeries, simple_moving_average
from app.domain.money import CALCULATION_PRECISION

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
TWO_PLACES = Decimal("0.01")


class AllocationError(DomainError):
    """The rule cannot be evaluated over this window."""


class AllocationRule(Protocol):
    """Decides, for each bar, whether the next bar should be held."""

    @property
    def name(self) -> str: ...

    @property
    def warmup_bars(self) -> int:
        """Bars needed before the rule produces a decision at all."""
        ...

    def signals(self, window: CandleWindow) -> Sequence[bool | None]:
        """One decision per bar. ``None`` while the rule is not warm.

        ``signals[i]`` must depend only on bars ``0..i``.
        """
        ...


@dataclass(frozen=True, slots=True)
class MovingAverageRegime:
    """Hold while the close is above a simple moving average of itself.

    The simplest regime filter there is, and deliberately so: every additional
    condition is another parameter to fit, and the point of this rule is that it
    has almost nothing to fit.
    """

    period: int = 100

    def __post_init__(self) -> None:
        if self.period < 2:
            raise AllocationError("A moving-average regime needs a period of at least 2")

    @property
    def name(self) -> str:
        return f"SMA {self.period}"

    @property
    def warmup_bars(self) -> int:
        return self.period

    def signals(self, window: CandleWindow) -> Sequence[bool | None]:
        average: IndicatorSeries = simple_moving_average(window.closes, self.period)
        return [
            None if level is None else close > level
            for close, level in zip(window.closes, average, strict=True)
        ]


@dataclass(frozen=True, slots=True)
class AllocationResult:
    """What a rule did, with the benchmark it must be judged against."""

    name: str
    bars: int
    total_return_percent: Decimal
    max_drawdown_percent: Decimal
    switches: int
    time_invested_percent: Decimal
    fees_paid_percent: Decimal

    @property
    def return_per_unit_of_drawdown(self) -> Decimal | None:
        """The comparison that survives different exposure levels."""
        if self.max_drawdown_percent <= ZERO:
            return None
        return (self.total_return_percent / self.max_drawdown_percent).quantize(Decimal("0.001"))

    def beats(self, benchmark: AllocationResult) -> tuple[bool, str]:
        """Whether this rule is better on BOTH return and drawdown.

        Both, not either. A rule that earns more while risking more has not
        improved anything - it has taken a larger position, which anyone can do
        without a rule.
        """
        better_return = self.total_return_percent > benchmark.total_return_percent
        better_risk = self.max_drawdown_percent < benchmark.max_drawdown_percent
        if better_return and better_risk:
            return True, (
                f"{self.name} beats {benchmark.name} on both: "
                f"{self.total_return_percent}% vs {benchmark.total_return_percent}% "
                f"return, {self.max_drawdown_percent}% vs "
                f"{benchmark.max_drawdown_percent}% drawdown."
            )
        if better_return:
            return False, (
                f"{self.name} earns more but risks more. A larger position does "
                f"that too, and needs no rule."
            )
        if better_risk:
            return False, (
                f"{self.name} risks less but earns less "
                f"({self.total_return_percent}% vs {benchmark.total_return_percent}%). "
                f"Whether that trade is worth taking is a decision, not a result."
            )
        return False, f"{self.name} is worse than {benchmark.name} on both measures."


def simulate_allocation(
    window: CandleWindow,
    rule: AllocationRule,
    *,
    fee_per_side: Decimal,
    start_at: int | None = None,
) -> AllocationResult:
    """Run one allocation rule over a window.

    ``start_at`` exists so several rules with different warm-up lengths can be
    scored over exactly the same bars. Without it, a slow rule and a fast one
    would be compared over different periods, and the difference between them
    would partly be the difference between two stretches of market.
    """
    signals = rule.signals(window)
    first = rule.warmup_bars if start_at is None else start_at
    if first >= len(window) - 1:
        raise AllocationError(f"{rule.name} needs more than {len(window)} bars to produce anything")

    with localcontext() as arithmetic:
        arithmetic.prec = CALCULATION_PRECISION
        closes = window.closes
        equity = ONE
        peak = ONE
        worst = ZERO
        fees = ZERO
        switches = 0
        invested = 0
        holding = False
        bars = 0

        for index in range(first, len(closes) - 1):
            bars += 1
            wanted = bool(signals[index])
            if wanted != holding:
                switches += 1
                equity *= ONE - fee_per_side
                fees += fee_per_side
                holding = wanted
            if holding:
                invested += 1
                # The return of bar index+1, earned by a position decided at the
                # close of bar index.
                equity *= closes[index + 1] / closes[index]
            peak = max(peak, equity)
            worst = min(worst, equity / peak - ONE)

        return AllocationResult(
            name=rule.name,
            bars=bars,
            total_return_percent=((equity - ONE) * HUNDRED).quantize(TWO_PLACES),
            max_drawdown_percent=((-worst) * HUNDRED).quantize(TWO_PLACES),
            switches=switches,
            time_invested_percent=(Decimal(invested) / Decimal(bars) * HUNDRED).quantize(
                Decimal("0.1")
            ),
            fees_paid_percent=(fees * HUNDRED).quantize(TWO_PLACES),
        )


@dataclass(frozen=True, slots=True)
class AlwaysInvested:
    """Buy and hold, expressed as an allocation rule.

    Written as a rule rather than as a special case so it runs through exactly
    the same simulation, pays a fee the same way and is measured over the same
    bars. A benchmark computed by a different code path is a benchmark that can
    disagree with the thing it benchmarks.
    """

    @property
    def name(self) -> str:
        return "BUY AND HOLD"

    @property
    def warmup_bars(self) -> int:
        return 0

    def signals(self, window: CandleWindow) -> Sequence[bool | None]:
        return [True] * len(window)
