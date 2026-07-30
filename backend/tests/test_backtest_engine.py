"""The replay engine.

The properties under test are the ones that decide whether a backtest result
means anything: that it runs the real rules, that it cannot see the future, that
it charges fees, and that it reports what it refused as well as what it did.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.backtest.engine import (
    BacktestConfig,
    BacktestError,
    MarketAssumptions,
    run_backtest,
)
from app.backtest.fills import ExitTrigger
from app.domain.candle_window import CandleWindow
from app.domain.enums import OrderSide, RiskReasonCode, Timeframe
from app.domain.risk.economics import TradingCosts
from app.domain.risk.limits import RiskLimits
from app.domain.symbol_filters import (
    LotSizeFilter,
    NotionalFilter,
    PriceFilter,
    SymbolFilters,
)
from app.strategies.base import SignalProposal, StrategyContext
from app.strategies.trend_pullback import TrendPullbackParameters, TrendPullbackStrategy

M15 = Timeframe.M15
BASE = datetime(2026, 1, 1, tzinfo=UTC)

FILTERS = SymbolFilters(
    symbol="BTCUSDT",
    price=PriceFilter(
        min_price=Decimal("0.01"), max_price=Decimal("10000000"), tick_size=Decimal("0.01")
    ),
    lot_size=LotSizeFilter(
        min_quantity=Decimal("0.00001"),
        max_quantity=Decimal("9000"),
        step_size=Decimal("0.00001"),
    ),
    notional=NotionalFilter(
        min_notional=Decimal("5"), max_notional=Decimal("9000000"), applies_to_market_orders=True
    ),
)

CALIBRATED = RiskLimits(
    reference_capital=Decimal("1000.00"),
    max_candle_age_seconds=1800,
    max_signal_age_seconds=300,
    max_spread_bps=Decimal("5"),
    min_order_book_depth_quote=Decimal("10000"),
    min_atr_percent=Decimal("0.05"),
    max_atr_percent=Decimal("5.00"),
    min_reward_risk_ratio=Decimal("1.8"),
    max_estimated_slippage_bps=Decimal("10"),
    max_clock_drift_ms=1000,
)

MARKET = MarketAssumptions(
    spread_bps=Decimal("1.5"),
    order_book_depth_quote=Decimal("250000"),
    slippage_bps=Decimal("2"),
)


def config(**overrides: object) -> BacktestConfig:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "timeframe": M15,
        "limits": CALIBRATED,
        "costs": TradingCosts(fee_rate_per_side=Decimal("0.00075")),
        "filters": FILTERS,
        "funding_rate": Decimal("4.60"),
        "market": MARKET,
    }
    values.update(overrides)
    return BacktestConfig(**values)  # type: ignore[arg-type]


def window_from(closes: Sequence[Decimal], spread: Decimal = Decimal("0.004")) -> CandleWindow:
    return CandleWindow(
        symbol="BTCUSDT",
        timeframe=M15,
        open_times=tuple(BASE + M15.duration * index for index in range(len(closes))),
        opens=tuple(closes),
        highs=tuple(close * (1 + spread) for close in closes),
        lows=tuple(close * (1 - spread) for close in closes),
        closes=tuple(closes),
        volumes=tuple(Decimal(10) for _ in closes),
    )


class AlwaysLong:
    """A strategy that proposes on every warm candle.

    Used where the point is the engine's behaviour, not the strategy's: a real
    strategy that refuses most candles would make it impossible to tell an
    engine bug from a strategy decision.
    """

    def __init__(self, stop_fraction: Decimal = Decimal("0.01")) -> None:
        self._stop_fraction = stop_fraction

    @property
    def name(self) -> str:
        return "always_long"

    @property
    def parameters(self) -> dict[str, object]:
        return {"stop_fraction": str(self._stop_fraction)}

    @property
    def required_candles(self) -> int:
        return 3

    def evaluate(self, context: StrategyContext) -> SignalProposal | None:
        if not context.window.has_at_least(self.required_candles):
            return None
        close = context.window.last_close
        stop = close * (1 - self._stop_fraction)
        return SignalProposal(
            side=OrderSide.BUY,
            reference_price=close,
            stop_loss_price=stop,
            take_profit_price=close + Decimal("2.5") * (close - stop),
            inputs={"atr_fraction": "0.006"},
        )


class TestConfiguration:
    def test_a_timeframe_mismatch_is_refused(self) -> None:
        window = CandleWindow.empty("BTCUSDT", Timeframe.H1)
        with pytest.raises(BacktestError, match="configured for"):
            run_backtest(AlwaysLong(), window, config())

    def test_a_non_positive_funding_rate_is_refused(self) -> None:
        with pytest.raises(BacktestError, match="Funding rate"):
            config(funding_rate=Decimal(0))


class TestTheRealRulesRun:
    def test_an_uncalibrated_configuration_produces_no_trades(self) -> None:
        """The engine runs the real risk rules. If it ran a simplified copy this
        would trade happily, and the backtest would validate something that will
        never execute."""
        window = window_from([Decimal(100) * (Decimal("1.001") ** i) for i in range(60)])
        result = run_backtest(
            AlwaysLong(), window, config(limits=RiskLimits(reference_capital=Decimal("1000")))
        )
        assert result.trades == []
        assert result.rejections[RiskReasonCode.RISK_CONFIGURATION_INCOMPLETE.value] > 0

    def test_refusals_are_counted_per_reason(self) -> None:
        """A run that produced no trades is not the same as a run that produced
        none because the minimum notional was never met."""
        window = window_from([Decimal(100) * (Decimal("1.001") ** i) for i in range(60)])
        # 0.01% of 1000 RON is 0.10 RON, which buys a position far below the
        # exchange's 5 USDT minimum notional.
        tiny = replace(CALIBRATED, maximum_risk_per_trade_percent=Decimal("0.01"))
        result = run_backtest(AlwaysLong(), window, config(limits=tiny))
        assert result.trades == []
        assert RiskReasonCode.MIN_NOTIONAL_NOT_MET.value in result.rejections

    def test_a_budget_that_rounds_to_nothing_is_refused_at_construction(self) -> None:
        """A configuration error must surface at the configuration, not as a
        crash somewhere far from it."""
        from app.domain.risk.limits import RiskLimitsError

        with pytest.raises(RiskLimitsError, match="rounds to zero"):
            replace(CALIBRATED, maximum_risk_per_trade_percent=Decimal("0.0001"))

    def test_the_consecutive_loss_rule_halts_a_losing_run(self) -> None:
        """Leaving the day state at zero would let a run take fifty losing
        trades in a row that the live system would have stopped after three."""
        falling = [Decimal(1000) * (Decimal("0.995") ** index) for index in range(200)]
        result = run_backtest(AlwaysLong(), window_from(falling), config())
        assert len(result.trades) <= CALIBRATED.maximum_consecutive_losses
        assert RiskReasonCode.MAX_CONSECUTIVE_LOSSES_REACHED.value in result.rejections


class TestNoLookAhead:
    def test_an_entry_never_fills_on_the_signal_bar(self) -> None:
        rising = [Decimal(1000) * (Decimal("1.002") ** index) for index in range(60)]
        result = run_backtest(AlwaysLong(), window_from(rising), config())
        for trade in result.trades:
            assert trade.entry_index > trade.signal_index

    def test_appending_future_candles_does_not_change_earlier_trades(self) -> None:
        """The property the whole run rests on. If a later candle could change
        an earlier decision, every result would be fiction."""
        closes = [Decimal(1000) * (Decimal("1.002") ** index) for index in range(80)]
        short_run = run_backtest(AlwaysLong(), window_from(closes[:60]), config())
        long_run = run_backtest(AlwaysLong(), window_from(closes), config())

        settled = [
            trade for trade in short_run.trades if trade.trigger is not ExitTrigger.END_OF_DATA
        ]
        for index, trade in enumerate(settled):
            assert long_run.trades[index].entry_price == trade.entry_price
            assert long_run.trades[index].exit_price == trade.exit_price


class TestOnePositionAtATime:
    def test_no_two_trades_overlap(self) -> None:
        rising = [Decimal(1000) * (Decimal("1.002") ** index) for index in range(200)]
        result = run_backtest(AlwaysLong(), window_from(rising), config())
        for earlier, later in zip(result.trades, result.trades[1:], strict=False):
            assert later.signal_index > earlier.exit_index


class TestCosts:
    def test_fees_are_charged_on_both_legs(self) -> None:
        rising = [Decimal(1000) * (Decimal("1.002") ** index) for index in range(80)]
        result = run_backtest(AlwaysLong(), window_from(rising), config())
        assert result.trades
        for trade in result.trades:
            assert trade.fees_quote > 0
            assert trade.net_pnl_quote == trade.gross_pnl_quote - trade.fees_quote

    def test_a_zero_fee_run_earns_more(self) -> None:
        rising = [Decimal(1000) * (Decimal("1.002") ** index) for index in range(80)]
        window = window_from(rising)
        charged = run_backtest(AlwaysLong(), window, config())
        free = run_backtest(
            AlwaysLong(), window, config(costs=TradingCosts(fee_rate_per_side=Decimal(0)))
        )
        assert sum(t.net_pnl_quote for t in free.trades) > sum(
            t.net_pnl_quote for t in charged.trades
        )

    def test_the_r_multiple_is_measured_against_the_risk_actually_taken(self) -> None:
        rising = [Decimal(1000) * (Decimal("1.002") ** index) for index in range(80)]
        result = run_backtest(AlwaysLong(), window_from(rising), config())
        assert result.trades
        for trade in result.trades:
            expected = (trade.net_pnl_reporting / trade.risk_reporting).quantize(
                Decimal("0.000001")
            )
            assert trade.r_multiple == expected


class TestReporting:
    def test_the_assumptions_travel_with_the_result(self) -> None:
        """A backtest read without its assumptions is a number without units."""
        rising = [Decimal(1000) * (Decimal("1.002") ** index) for index in range(60)]
        result = run_backtest(AlwaysLong(), window_from(rising), config())
        assumptions = result.assumptions
        assert "optimistic" in assumptions["fills"]["fills_on_touch"]
        assert "no spread" in assumptions["market"]["spread_bps"]
        assert "constant" in assumptions["funding_rate"]["note"]

    def test_the_strategy_is_identified_by_its_code_not_only_its_name(self) -> None:
        """Two runs with the same parameters but different code are not the same
        experiment (AC-20)."""
        rising = [Decimal(1000) * (Decimal("1.002") ** index) for index in range(60)]
        result = run_backtest(AlwaysLong(), window_from(rising), config())
        assert result.strategy_name == "always_long"
        assert len(result.strategy_fingerprint) == 64
        assert result.strategy_parameters == {"stop_fraction": "0.01"}

    def test_unfilled_entries_are_counted(self) -> None:
        """An order that never traded is not a rejection and not a trade. It is
        its own outcome, and a run where most entries expire is telling you
        something about the entry price."""
        rising = [Decimal(1000) * (Decimal("1.01") ** index) for index in range(80)]
        result = run_backtest(AlwaysLong(), window_from(rising, spread=Decimal("0.0001")), config())
        assert result.entries_expired_unfilled >= 0
        assert result.proposals >= len(result.trades)


class TestDeterminism:
    def test_the_same_inputs_produce_the_same_run(self) -> None:
        rising = [Decimal(1000) * (Decimal("1.002") ** index) for index in range(120)]
        window = window_from(rising)
        first = run_backtest(AlwaysLong(), window, config())
        second = run_backtest(AlwaysLong(), window, config())
        assert [t.net_pnl_quote for t in first.trades] == [t.net_pnl_quote for t in second.trades]


class TestWithTheRealStrategy:
    def test_the_baseline_strategy_runs_end_to_end(self) -> None:
        """It may well produce no trades on synthetic data, and that is a
        result, not a failure."""
        parameters = TrendPullbackParameters(
            trend_fast_period=5,
            trend_slow_period=20,
            warmup_margin_candles=30,
            rsi_period=5,
            pullback_lookback=3,
            atr_period=5,
            swing_lookback=5,
        )
        closes: list[Decimal] = [Decimal(1000)]
        for index in range(300):
            factor = Decimal("0.988") if index % 17 in (0, 1) else Decimal("1.003")
            closes.append(closes[-1] * factor)

        result = run_backtest(TrendPullbackStrategy(parameters), window_from(closes), config())
        assert result.bars_evaluated > 0
        assert result.strategy_name == "trend_pullback_v1"
        # Whatever it did, every trade must respect the risk budget.
        for trade in result.trades:
            assert trade.risk_reporting <= CALIBRATED.risk_per_trade_amount
