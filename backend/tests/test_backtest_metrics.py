"""Reading a backtest honestly.

Most of these tests are about refusing to over-read a number. The arithmetic is
the easy part; the discipline is in what the module declines to claim.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.backtest.engine import BacktestTrade
from app.backtest.fills import ExitTrigger
from app.backtest.metrics import (
    MINIMUM_TRADES_FOR_ANY_CONCLUSION,
    SampleAdequacy,
    _compute,
)
from app.domain.enums import OrderSide

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def trade(
    r: str,
    *,
    index: int = 0,
    trigger: ExitTrigger = ExitTrigger.TAKE_PROFIT,
    gapped: bool = False,
    ambiguous: bool = False,
) -> BacktestTrade:
    r_multiple = Decimal(r)
    risk = Decimal("5.00")
    net = r_multiple * risk
    return BacktestTrade(
        signal_index=index,
        entry_index=index + 1,
        exit_index=index + 2,
        entry_time=BASE + timedelta(hours=index),
        exit_time=BASE + timedelta(hours=index + 1),
        side=OrderSide.BUY,
        quantity=Decimal("0.001"),
        entry_price=Decimal("65000"),
        exit_price=Decimal("65000") + net,
        stop_loss_price=Decimal("64350"),
        take_profit_price=Decimal("66625"),
        trigger=trigger,
        gross_pnl_quote=net,
        fees_quote=Decimal("0.10"),
        net_pnl_quote=net,
        net_pnl_reporting=net,
        r_multiple=r_multiple,
        risk_reporting=risk,
        gapped=gapped,
        ambiguous_bar=ambiguous,
    )


def series(*multiples: str) -> list[BacktestTrade]:
    return [trade(value, index=index) for index, value in enumerate(multiples)]


def repeated(pattern: list[str], times: int) -> list[BacktestTrade]:
    values = (pattern * times)[: len(pattern) * times]
    return [trade(value, index=index) for index, value in enumerate(values)]


class TestSampleAdequacy:
    def test_twelve_profitable_trades_are_not_evidence(self) -> None:
        """The headline rule of the module. Twelve wins are a coincidence with
        a flattering shape."""
        metrics = _compute(series(*["2.5"] * 12))
        assert metrics.is_profitable is True
        assert metrics.adequacy is SampleAdequacy.INSUFFICIENT
        assert metrics.has_a_demonstrated_edge is False

    def test_the_thresholds_are_stated_not_implied(self) -> None:
        assert SampleAdequacy.of(29) is SampleAdequacy.INSUFFICIENT
        assert SampleAdequacy.of(30) is SampleAdequacy.WEAK
        assert SampleAdequacy.of(100) is SampleAdequacy.TENTATIVE
        assert SampleAdequacy.of(300) is SampleAdequacy.REASONABLE

    def test_only_the_larger_samples_support_a_conclusion(self) -> None:
        assert SampleAdequacy.WEAK.supports_a_conclusion is False
        assert SampleAdequacy.TENTATIVE.supports_a_conclusion is True

    def test_even_a_large_sample_is_qualified(self) -> None:
        """Evidence about the regime the data covered is not a promise about
        the next one."""
        note = SampleAdequacy.REASONABLE.explain(500)
        assert "regime" in note

    def test_the_summary_leads_with_the_caveat(self) -> None:
        metrics = _compute(series("2.5", "-1", "2.5"))
        assert metrics.summary().startswith("Sample: INSUFFICIENT")


class TestWinRateInterval:
    def test_a_small_sample_interval_is_wide_enough_to_be_useless(self) -> None:
        """60% from 15 trades spans "no edge" and "excellent" at once."""
        metrics = _compute(repeated(["2.5", "2.5", "2.5", "-1", "-1"], 3))
        assert metrics.win_rate.estimate == Decimal("60.0000")
        assert metrics.win_rate.low < Decimal("40")
        assert metrics.win_rate.high > Decimal("77")
        assert metrics.win_rate.is_reliable is False

    def test_the_interval_never_leaves_the_zero_to_hundred_range(self) -> None:
        """The textbook normal approximation runs past 100%. Wilson does not."""
        metrics = _compute(series(*["2.5"] * 10))
        assert metrics.win_rate.low >= Decimal(0)
        assert metrics.win_rate.high <= Decimal(100)

    def test_an_all_wins_sample_still_gets_a_wide_interval(self) -> None:
        """The normal approximation collapses to zero width here - exactly the
        case where an honest interval matters most."""
        metrics = _compute(series(*["2.5"] * 8))
        assert metrics.win_rate.estimate == Decimal("100.0000")
        assert metrics.win_rate.low < Decimal("70")

    def test_more_trades_narrow_the_interval(self) -> None:
        small = _compute(repeated(["2.5", "-1"], 10))
        large = _compute(repeated(["2.5", "-1"], 200))
        small_width = small.win_rate.high - small.win_rate.low
        large_width = large.win_rate.high - large.win_rate.low
        assert large_width < small_width


class TestExpectancy:
    def test_a_profitable_run_without_a_demonstrated_edge_says_so(self) -> None:
        """Profitable and "has an edge" are different claims."""
        # A high win rate but enormous variance: positive mean, interval spans zero.
        trades = repeated(["6", "-1", "-1", "-1", "-1", "-1"], 20)
        metrics = _compute(trades)
        assert metrics.total_r > 0
        if metrics.expectancy_r.includes_zero:
            assert metrics.has_a_demonstrated_edge is False

    def test_a_clear_edge_over_many_trades_is_recognised(self) -> None:
        trades = repeated(["2.5", "-1"], 200)
        metrics = _compute(trades)
        assert metrics.expectancy_r.estimate > 0
        assert metrics.expectancy_r.includes_zero is False
        assert metrics.has_a_demonstrated_edge is True

    def test_the_approximation_is_named_as_one(self) -> None:
        """R multiples are bounded below and right-skewed, so the interval is
        indicative rather than exact."""
        metrics = _compute(repeated(["2.5", "-1"], 50))
        assert "normal approximation" in metrics.expectancy_r.method
        assert "skewed" in metrics.expectancy_r.caveat

    def test_a_thin_sample_is_marked_do_not_act_on_this(self) -> None:
        metrics = _compute(series("2.5", "-1", "2.5"))
        assert metrics.expectancy_r.is_reliable is False
        assert "Do not act" in metrics.expectancy_r.caveat

    def test_one_trade_has_no_spread_to_estimate(self) -> None:
        metrics = _compute(series("2.5"))
        assert metrics.expectancy_r.low == metrics.expectancy_r.high
        assert metrics.expectancy_r.is_reliable is False


class TestTotalsAndCurve:
    def test_the_equity_curve_is_cumulative_r(self) -> None:
        metrics = _compute(series("1", "-1", "2"))
        assert metrics.equity_curve_r == (Decimal(1), Decimal(0), Decimal(2))
        assert metrics.total_r == Decimal("2.0000")

    def test_the_drawdown_is_measured_from_the_running_peak(self) -> None:
        """What matters is the worst stretch an operator would have sat
        through, not the distance below zero."""
        metrics = _compute(series("5", "-1", "-1", "-1", "3"))
        assert metrics.max_drawdown_r == Decimal("3.0000")

    def test_a_run_that_only_rises_has_no_drawdown(self) -> None:
        metrics = _compute(series("1", "1", "1"))
        assert metrics.max_drawdown_r == Decimal("0.0000")

    def test_streaks_are_counted_in_both_directions(self) -> None:
        metrics = _compute(series("1", "-1", "-1", "-1", "1", "1"))
        assert metrics.longest_losing_streak == 3
        assert metrics.longest_winning_streak == 2

    def test_the_worst_and_best_single_trades_are_reported(self) -> None:
        metrics = _compute(series("1", "-2.4", "3.1"))
        assert metrics.largest_win_r == Decimal("3.1")
        assert metrics.largest_loss_r == Decimal("-2.4")


class TestProfitFactor:
    def test_it_is_gross_profit_over_gross_loss(self) -> None:
        metrics = _compute(series("2", "2", "-1"))
        assert metrics.profit_factor == Decimal("4.0000")

    def test_a_sample_with_no_losses_reports_none_not_infinity(self) -> None:
        """ "Infinite profit factor" is not a measurement, it is a sample with
        no losses in it yet."""
        metrics = _compute(series("2", "3"))
        assert metrics.profit_factor is None


class TestBreakdowns:
    def test_trades_are_grouped_by_exit_reason(self) -> None:
        trades = [
            trade("2.5", index=0, trigger=ExitTrigger.TAKE_PROFIT),
            trade("-1", index=1, trigger=ExitTrigger.STOP_LOSS),
            trade("-1", index=2, trigger=ExitTrigger.STOP_LOSS),
        ]
        metrics = _compute(trades)
        assert metrics.by_exit_trigger["STOP_LOSS"].trade_count == 2
        assert metrics.by_exit_trigger["TAKE_PROFIT"].total_r == Decimal("2.5000")

    def test_trades_are_grouped_by_month(self) -> None:
        metrics = _compute(series("1", "-1"))
        assert "2026-01" in metrics.by_month
        assert metrics.by_month["2026-01"].trade_count == 2

    def test_a_breakdown_reports_its_own_win_rate(self) -> None:
        metrics = _compute(series("1", "-1", "1", "1"))
        january = metrics.by_month["2026-01"]
        assert january.win_rate == Decimal("75.0000")


class TestSimulationAssumptionsAreSurfaced:
    def test_gapped_and_ambiguous_exits_are_counted(self) -> None:
        """Both are places the simulation had to assume something, and a run
        that leans on them heavily is a run to distrust."""
        trades = [
            trade("-1.4", index=0, trigger=ExitTrigger.STOP_LOSS, gapped=True),
            trade("-1", index=1, trigger=ExitTrigger.STOP_LOSS, ambiguous=True),
            trade("2.5", index=2),
        ]
        metrics = _compute(trades)
        assert metrics.gapped_exits == 1
        assert metrics.ambiguous_exits == 1
        assert "load-bearing" in metrics.summary()

    def test_a_negative_edge_is_reported_as_evidence_not_as_absence(self) -> None:
        """A strategy whose interval sits entirely below zero has not failed to
        demonstrate an edge - it has demonstrated a negative one, and that calls
        for a different decision."""
        metrics = _compute(repeated(["-1", "-1", "-1", "-1", "1"], 60))
        assert metrics.expectancy_r.estimate < 0
        assert metrics.expectancy_r.includes_zero is False
        assert "DEMONSTRATED NEGATIVE EDGE" in metrics.verdict
        assert "it is evidence" in metrics.verdict

    def test_an_inconclusive_run_says_so_in_both_directions(self) -> None:
        metrics = _compute(series("2.5", "-1", "2.5"))
        assert "INCONCLUSIVE" in metrics.verdict
        assert "either direction" in metrics.verdict


class TestEmptyRun:
    def test_a_run_with_no_trades_claims_nothing(self) -> None:
        metrics = _compute([])
        assert metrics.trade_count == 0
        assert metrics.adequacy is SampleAdequacy.INSUFFICIENT
        assert metrics.has_a_demonstrated_edge is False
        assert metrics.profit_factor is None
        assert metrics.equity_curve_r == ()

    def test_the_minimum_is_a_named_constant_not_a_magic_number(self) -> None:
        assert MINIMUM_TRADES_FOR_ANY_CONCLUSION == 30
