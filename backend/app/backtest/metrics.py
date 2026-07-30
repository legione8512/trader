"""Reading a backtest honestly.

Every figure here is easy to compute and easy to over-read, so the module is
built around one rule:

    **No performance figure is reported without the number of trades behind it
    and an interval around it.**

Twelve profitable trades are not evidence, they are a coincidence with a
flattering shape. A win rate of 60% from 15 trades has a 95% interval running
from roughly 35% to 80% - which includes "no edge at all" and also "excellent" -
and a report that prints "60%" without that range invites a decision the data
cannot support.

Results are stated in **R**, units of the risk actually taken, as the primary
measure. Currency totals depend on the funding rate, which a backtest holds
constant and reality does not; R is the same number whatever the exchange rate
did, and it is what compares two runs of different sizes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal, localcontext
from enum import StrEnum

from app.backtest.engine import BacktestResult, BacktestTrade
from app.domain.money import CALCULATION_PRECISION

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)
#: 95% two-sided normal quantile.
Z_95 = Decimal("1.959964")
FOUR_PLACES = Decimal("0.0001")

#: Below this, no performance figure means anything at all. Not a rule of thumb
#: dressed up as a law - simply the point below which the interval around any
#: estimate is wider than the range of conclusions it could support.
MINIMUM_TRADES_FOR_ANY_CONCLUSION = 30
TRADES_FOR_A_TENTATIVE_CONCLUSION = 100
TRADES_FOR_A_REASONABLE_CONCLUSION = 300


class SampleAdequacy(StrEnum):
    """How much weight the numbers can carry."""

    #: Fewer than 30 trades. Report the numbers, conclude nothing.
    INSUFFICIENT = "INSUFFICIENT"
    #: 30 to 99. A direction, not a measurement.
    WEAK = "WEAK"
    #: 100 to 299. Enough to rule things out, not enough to size a decision on.
    TENTATIVE = "TENTATIVE"
    #: 300 or more, and still only for the regime the data covered.
    REASONABLE = "REASONABLE"

    @classmethod
    def of(cls, trade_count: int) -> SampleAdequacy:
        if trade_count < MINIMUM_TRADES_FOR_ANY_CONCLUSION:
            return cls.INSUFFICIENT
        if trade_count < TRADES_FOR_A_TENTATIVE_CONCLUSION:
            return cls.WEAK
        if trade_count < TRADES_FOR_A_REASONABLE_CONCLUSION:
            return cls.TENTATIVE
        return cls.REASONABLE

    @property
    def supports_a_conclusion(self) -> bool:
        return self in {SampleAdequacy.TENTATIVE, SampleAdequacy.REASONABLE}

    def explain(self, trade_count: int) -> str:
        return {
            SampleAdequacy.INSUFFICIENT: (
                f"{trade_count} trades. Below {MINIMUM_TRADES_FOR_ANY_CONCLUSION} no "
                f"performance figure supports a conclusion; the interval around every "
                f"estimate is wider than the range of answers it could distinguish."
            ),
            SampleAdequacy.WEAK: (
                f"{trade_count} trades. Enough to see a direction, not enough to "
                f"measure one. Treat every figure below as provisional."
            ),
            SampleAdequacy.TENTATIVE: (
                f"{trade_count} trades. Enough to rule some things out, not enough "
                f"to size a decision on."
            ),
            SampleAdequacy.REASONABLE: (
                f"{trade_count} trades - and still only evidence about the market "
                f"regime this data covered, which is not a promise about the next one."
            ),
        }[self]


@dataclass(frozen=True, slots=True)
class Interval:
    """An estimate with the range it could plausibly be.

    Carried beside every headline figure. An estimate without one reads as a
    fact, and none of these are facts.
    """

    estimate: Decimal
    low: Decimal
    high: Decimal
    method: str
    is_reliable: bool
    caveat: str = ""

    @property
    def includes_zero(self) -> bool:
        """Whether "no edge" is inside the range. Usually the only question
        that matters."""
        return self.low <= ZERO <= self.high

    def __str__(self) -> str:
        return f"{self.estimate} [{self.low}, {self.high}]"


@dataclass(frozen=True, slots=True)
class PeriodBreakdown:
    """One slice of the run - a month, an hour of day, an exit reason."""

    label: str
    trade_count: int
    total_r: Decimal
    wins: int

    @property
    def win_rate(self) -> Decimal:
        if self.trade_count == 0:
            return ZERO
        return (Decimal(self.wins) / Decimal(self.trade_count) * HUNDRED).quantize(FOUR_PLACES)


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """Everything the run says, with the weight each figure can carry."""

    trade_count: int
    adequacy: SampleAdequacy
    adequacy_note: str

    wins: int
    losses: int
    win_rate: Interval
    expectancy_r: Interval

    total_r: Decimal
    total_reporting: Decimal
    total_fees_quote: Decimal
    profit_factor: Decimal | None

    max_drawdown_r: Decimal
    longest_losing_streak: int
    longest_winning_streak: int
    largest_win_r: Decimal
    largest_loss_r: Decimal

    #: Cumulative R after each trade. The curve, not a summary of it.
    equity_curve_r: tuple[Decimal, ...]

    by_exit_trigger: dict[str, PeriodBreakdown] = field(default_factory=dict)
    by_month: dict[str, PeriodBreakdown] = field(default_factory=dict)

    #: Trades whose exit came from a gap, and trades whose bar touched both
    #: levels. Both are places the simulation had to assume something.
    gapped_exits: int = 0
    ambiguous_exits: int = 0

    @property
    def is_profitable(self) -> bool:
        return self.total_r > ZERO

    @property
    def has_a_demonstrated_edge(self) -> bool:
        """Profitable is not the same as having an edge.

        An edge means the interval around the expectancy excludes zero AND
        there were enough trades for that interval to mean anything. A run can
        be profitable and have neither.
        """
        return (
            self.adequacy.supports_a_conclusion
            and self.expectancy_r.is_reliable
            and not self.expectancy_r.includes_zero
            and self.expectancy_r.estimate > ZERO
        )

    def summary(self) -> str:
        """A report that leads with the caveat rather than burying it."""
        lines = [
            f"Sample: {self.adequacy.value}. {self.adequacy_note}",
            f"Trades: {self.trade_count} ({self.wins}W / {self.losses}L)",
            f"Win rate: {self.win_rate}% ({self.win_rate.method})",
            f"Expectancy: {self.expectancy_r} R per trade ({self.expectancy_r.method})",
            f"Total: {self.total_r} R, {self.total_reporting} reporting currency",
            f"Max drawdown: {self.max_drawdown_r} R",
            f"Longest losing streak: {self.longest_losing_streak}",
        ]
        if self.profit_factor is not None:
            lines.append(f"Profit factor: {self.profit_factor}")
        if not self.has_a_demonstrated_edge:
            lines.append(
                "NO DEMONSTRATED EDGE: the expectancy interval includes zero, or "
                "there are too few trades for it to mean anything."
            )
        if self.gapped_exits or self.ambiguous_exits:
            lines.append(
                f"Simulation assumptions were load-bearing in "
                f"{self.gapped_exits} gapped and {self.ambiguous_exits} ambiguous exits."
            )
        return "\n".join(lines)


def compute_metrics(result: BacktestResult) -> BacktestMetrics:
    """Summarise a run, with the weight each figure can carry attached."""
    with localcontext() as arithmetic:
        arithmetic.prec = CALCULATION_PRECISION
        return _compute(result.trades)


def _compute(trades: Sequence[BacktestTrade]) -> BacktestMetrics:
    count = len(trades)
    adequacy = SampleAdequacy.of(count)

    if count == 0:
        empty = Interval(
            ZERO, ZERO, ZERO, "not computed", False, "No trades, so nothing to estimate."
        )
        return BacktestMetrics(
            trade_count=0,
            adequacy=adequacy,
            adequacy_note=adequacy.explain(0),
            wins=0,
            losses=0,
            win_rate=empty,
            expectancy_r=empty,
            total_r=ZERO,
            total_reporting=ZERO,
            total_fees_quote=ZERO,
            profit_factor=None,
            max_drawdown_r=ZERO,
            longest_losing_streak=0,
            longest_winning_streak=0,
            largest_win_r=ZERO,
            largest_loss_r=ZERO,
            equity_curve_r=(),
        )

    r_values = [trade.r_multiple for trade in trades]
    wins = sum(1 for trade in trades if trade.is_win)
    losses = count - wins

    equity: list[Decimal] = []
    running = ZERO
    for value in r_values:
        running += value
        equity.append(running)

    gross_profit = sum((v for v in r_values if v > ZERO), ZERO)
    gross_loss = -sum((v for v in r_values if v < ZERO), ZERO)

    return BacktestMetrics(
        trade_count=count,
        adequacy=adequacy,
        adequacy_note=adequacy.explain(count),
        wins=wins,
        losses=losses,
        win_rate=_win_rate_interval(wins, count),
        expectancy_r=_expectancy_interval(r_values),
        total_r=running.quantize(FOUR_PLACES),
        total_reporting=sum((t.net_pnl_reporting for t in trades), ZERO).quantize(FOUR_PLACES),
        total_fees_quote=sum((t.fees_quote for t in trades), ZERO).quantize(FOUR_PLACES),
        # None rather than infinity when nothing was lost. "Infinite profit
        # factor" is not a measurement, it is a sample with no losses in it yet.
        profit_factor=(
            (gross_profit / gross_loss).quantize(FOUR_PLACES) if gross_loss > ZERO else None
        ),
        max_drawdown_r=_max_drawdown(equity),
        longest_losing_streak=_longest_streak(trades, winning=False),
        longest_winning_streak=_longest_streak(trades, winning=True),
        largest_win_r=max(r_values),
        largest_loss_r=min(r_values),
        equity_curve_r=tuple(equity),
        by_exit_trigger=_group(trades, lambda t: t.trigger.value),
        by_month=_group(trades, lambda t: t.exit_time.strftime("%Y-%m")),
        gapped_exits=sum(1 for t in trades if t.gapped),
        ambiguous_exits=sum(1 for t in trades if t.ambiguous_bar),
    )


def _win_rate_interval(wins: int, count: int) -> Interval:
    """Wilson score interval.

    Not the textbook normal approximation. That one produces intervals that run
    past 100%, and collapses to zero width when a sample happens to be all wins
    or all losses - exactly the small samples where an honest interval matters
    most. Wilson stays inside [0, 1] and stays wide when the data is thin.
    """
    n = Decimal(count)
    p = Decimal(wins) / n
    z_squared = Z_95 * Z_95

    denominator = ONE + z_squared / n
    centre = (p + z_squared / (Decimal(2) * n)) / denominator
    spread = Z_95 / denominator * ((p * (ONE - p) / n) + z_squared / (Decimal(4) * n * n)).sqrt()

    reliable = count >= MINIMUM_TRADES_FOR_ANY_CONCLUSION
    return Interval(
        estimate=(p * HUNDRED).quantize(FOUR_PLACES),
        low=(max(ZERO, centre - spread) * HUNDRED).quantize(FOUR_PLACES),
        high=(min(ONE, centre + spread) * HUNDRED).quantize(FOUR_PLACES),
        method="Wilson score, 95%",
        is_reliable=reliable,
        caveat=(
            ""
            if reliable
            else f"Fewer than {MINIMUM_TRADES_FOR_ANY_CONCLUSION} trades: the interval "
            f"is honest but so wide it distinguishes nothing."
        ),
    )


def _expectancy_interval(r_values: Sequence[Decimal]) -> Interval:
    """Mean R per trade, with a normal-approximation interval.

    The approximation is named because it is one. R multiples are not normally
    distributed - they are bounded below near -1 and have a long right tail -
    so this interval is indicative rather than exact, and it is marked
    unreliable below the minimum sample size rather than quietly printed.
    """
    n = Decimal(len(r_values))
    mean = sum(r_values, ZERO) / n

    if len(r_values) < 2:
        return Interval(
            estimate=mean.quantize(FOUR_PLACES),
            low=mean.quantize(FOUR_PLACES),
            high=mean.quantize(FOUR_PLACES),
            method="single observation",
            is_reliable=False,
            caveat="One trade has no spread to estimate.",
        )

    variance = sum(((value - mean) ** 2 for value in r_values), ZERO) / (n - ONE)
    standard_error = variance.sqrt() / n.sqrt()
    margin = Z_95 * standard_error

    reliable = len(r_values) >= MINIMUM_TRADES_FOR_ANY_CONCLUSION
    return Interval(
        estimate=mean.quantize(FOUR_PLACES),
        low=(mean - margin).quantize(FOUR_PLACES),
        high=(mean + margin).quantize(FOUR_PLACES),
        method="normal approximation, 95%",
        is_reliable=reliable,
        caveat=(
            "R multiples are bounded below and right-skewed, so this interval is "
            "indicative rather than exact."
            if reliable
            else f"Fewer than {MINIMUM_TRADES_FOR_ANY_CONCLUSION} trades, and R "
            f"multiples are not normally distributed. Do not act on this range."
        ),
    )


def _max_drawdown(equity: Sequence[Decimal]) -> Decimal:
    """Deepest peak-to-trough fall on the cumulative R curve.

    Reported positive. Measured from the running peak rather than from the
    start, because what matters is the worst stretch a real operator would have
    had to sit through, not the distance below zero.
    """
    peak = ZERO
    worst = ZERO
    for value in equity:
        peak = max(peak, value)
        worst = min(worst, value - peak)
    return (-worst).quantize(FOUR_PLACES)


def _longest_streak(trades: Sequence[BacktestTrade], *, winning: bool) -> int:
    longest = 0
    current = 0
    for trade in trades:
        if trade.is_win is winning:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _group(trades: Sequence[BacktestTrade], key: object) -> dict[str, PeriodBreakdown]:
    buckets: dict[str, list[BacktestTrade]] = {}
    for trade in trades:
        label = key(trade)  # type: ignore[operator]
        buckets.setdefault(label, []).append(trade)
    return {
        label: PeriodBreakdown(
            label=label,
            trade_count=len(group),
            total_r=sum((t.r_multiple for t in group), ZERO).quantize(FOUR_PLACES),
            wins=sum(1 for t in group if t.is_win),
        )
        for label, group in sorted(buckets.items())
    }
