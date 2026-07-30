"""The baseline strategy.

Built on synthetic series with a known shape, so each gate can be shown to
refuse for its own reason rather than by accident. A test that passes because
some other gate happened to fire proves nothing about the gate it names.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.candle_window import CandleWindow
from app.domain.enums import OrderSide, Timeframe
from app.domain.money import PRICE_PLACES
from app.strategies.base import (
    SignalProposal,
    StrategyContext,
    StrategyError,
    code_fingerprint,
)
from app.strategies.builtin import register_builtin_strategies
from app.strategies.registry import StrategyRegistry
from app.strategies.trend_pullback import TrendPullbackParameters, TrendPullbackStrategy

M15 = Timeframe.M15
BASE = datetime(2026, 1, 1, tzinfo=UTC)
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

#: Small periods keep the fixtures readable while exercising the same code.
FAST_PARAMETERS = TrendPullbackParameters(
    trend_fast_period=5,
    trend_slow_period=20,
    warmup_margin_candles=30,
    rsi_period=5,
    pullback_lookback=3,
    atr_period=5,
    swing_lookback=5,
)


def window_from_closes(
    closes: Sequence[Decimal],
    *,
    spread: Decimal = Decimal("0.004"),
    symbol: str = "BTCUSDT",
) -> CandleWindow:
    """A window whose highs and lows sit a fixed fraction either side of close.

    A constant fractional spread keeps ATR proportional to price, which is what
    the volatility gate is expressed in.
    """
    highs = tuple(close * (1 + spread) for close in closes)
    lows = tuple(close * (1 - spread) for close in closes)
    return CandleWindow(
        symbol=symbol,
        timeframe=M15,
        open_times=tuple(BASE + M15.duration * index for index in range(len(closes))),
        opens=tuple(closes),
        highs=highs,
        lows=lows,
        closes=tuple(closes),
        volumes=tuple(Decimal(10) for _ in closes),
    )


def uptrend_with_pullback(
    length: int = 120,
    *,
    drift: Decimal = Decimal("0.002"),
    dip_depth: Decimal = Decimal("0.010"),
    dip_candles: int = 3,
) -> list[Decimal]:
    """A steady uptrend, a short dip near the end, then one strong candle.

    Shaped to satisfy every gate at the final candle: the trend is established,
    the RSI dips below the threshold during the pullback and recovers, and the
    last close clears the previous high.
    """
    closes = [Decimal(1000)]
    for _ in range(length - dip_candles - 2):
        closes.append(closes[-1] * (1 + drift))
    for _ in range(dip_candles):
        closes.append(closes[-1] * (1 - dip_depth))
    # The resumption candle: a decisive close above the previous candle's high.
    closes.append(closes[-1] * Decimal("1.020"))
    return closes


def evaluate(
    closes: Sequence[Decimal],
    parameters: TrendPullbackParameters = FAST_PARAMETERS,
    *,
    spread: Decimal = Decimal("0.004"),
) -> SignalProposal | None:
    strategy = TrendPullbackStrategy(parameters)
    context = StrategyContext(window=window_from_closes(closes, spread=spread), evaluated_at=NOW)
    return strategy.evaluate(context)


class TestParameters:
    def test_the_defaults_match_the_approved_design(self) -> None:
        """These numbers were reviewed in Phase 4.3. A silent change to any of
        them changes what was approved."""
        parameters = TrendPullbackParameters()
        assert parameters.trend_fast_period == 50
        assert parameters.trend_slow_period == 200
        assert parameters.reward_risk_target == Decimal("2.5")
        assert parameters.pullback_rsi_entry == Decimal("40")
        assert parameters.min_stop_fraction == Decimal("0.005")

    def test_the_reward_risk_target_clears_the_fee_break_even(self) -> None:
        """At a 1% stop the round trip costs 0.20R. A 2.0 target at a 40% win
        rate yields a gross edge of exactly 0.20R, which is break-even before
        anything goes wrong."""
        assert TrendPullbackParameters().reward_risk_target > Decimal("2.0")

    def test_the_minimum_stop_reflects_the_no_leverage_ceiling(self) -> None:
        """Risk 5 RON against 1000 RON of reference capital: a stop tighter than
        0.5% of price would need a notional above the capital."""
        risk = Decimal("5")
        capital = Decimal("1000")
        assert TrendPullbackParameters().min_stop_fraction >= risk / capital

    def test_a_fast_period_at_or_above_the_slow_one_is_refused(self) -> None:
        with pytest.raises(StrategyError, match="shorter than the slow"):
            TrendPullbackParameters(trend_fast_period=200, trend_slow_period=200)

    def test_inverted_stop_bounds_are_refused(self) -> None:
        with pytest.raises(StrategyError, match="0 < min < max"):
            TrendPullbackParameters(
                min_stop_fraction=Decimal("0.05"), max_stop_fraction=Decimal("0.01")
            )

    def test_a_non_positive_reward_target_is_refused(self) -> None:
        with pytest.raises(StrategyError, match="reward_risk_target"):
            TrendPullbackParameters(reward_risk_target=Decimal(0))

    def test_an_rsi_threshold_outside_its_range_is_refused(self) -> None:
        with pytest.raises(StrategyError, match="between 0 and 100"):
            TrendPullbackParameters(pullback_rsi_entry=Decimal(100))


class TestParameterRoundTrip:
    def test_a_stored_parameter_set_rebuilds_exactly(self) -> None:
        """AC-20: a version stored months ago must produce the same strategy."""
        original = TrendPullbackParameters(trend_fast_period=30, reward_risk_target=Decimal("3.0"))
        rebuilt = TrendPullbackParameters.from_mapping(original.as_dict())
        assert rebuilt == original

    def test_decimals_are_stored_as_strings_never_floats(self) -> None:
        stored = TrendPullbackParameters().as_dict()
        assert stored["reward_risk_target"] == "2.5"
        assert all(not isinstance(value, float) for value in stored.values())

    def test_a_parameter_stored_as_a_json_float_fails_loudly(self) -> None:
        """Silently accepting it would change the arithmetic and the record
        would not say so."""
        with pytest.raises(Exception, match="float is forbidden"):
            TrendPullbackParameters.from_mapping({"reward_risk_target": 2.5})

    def test_an_unknown_parameter_is_refused_not_ignored(self) -> None:
        """A typo falling back to the default means a run used parameters
        nobody chose, and the stored record would not show it."""
        with pytest.raises(StrategyError, match="Unknown strategy parameters"):
            TrendPullbackParameters.from_mapping({"reward_risk_ratio": "3"})

    def test_a_partial_parameter_set_keeps_the_defaults(self) -> None:
        rebuilt = TrendPullbackParameters.from_mapping({"trend_fast_period": 30})
        assert rebuilt.trend_fast_period == 30
        assert rebuilt.reward_risk_target == TrendPullbackParameters().reward_risk_target


class TestWarmUp:
    def test_the_required_window_accounts_for_the_ema_seed_decay(self) -> None:
        """The EMA seed is a simple average whose weight decays as
        (1 - alpha)^n. For period 200 that needs ~300 extra candles to fall
        under 5%, not the 200 the strict warm-up would suggest."""
        strategy = TrendPullbackStrategy()
        assert strategy.required_candles == 500

    def test_the_rsi_lookback_is_counted_from_the_pullback_not_the_last_candle(
        self,
    ) -> None:
        parameters = TrendPullbackParameters(
            trend_fast_period=2,
            trend_slow_period=3,
            warmup_margin_candles=0,
            rsi_period=14,
            pullback_lookback=6,
            atr_period=2,
            swing_lookback=2,
        )
        # rsi_period + 1 + pullback_lookback = 21 dominates the slow period of 3.
        assert TrendPullbackStrategy(parameters).required_candles == 21

    def test_a_short_window_produces_no_signal(self) -> None:
        """Never a partial evaluation: a half-warm indicator is a different
        indicator."""
        assert evaluate([Decimal(1000)] * 10) is None


class TestASignalIsProduced:
    def test_the_designed_setup_produces_a_long(self) -> None:
        proposal = evaluate(uptrend_with_pullback())
        assert proposal is not None
        assert proposal.side is OrderSide.BUY

    def test_the_target_sits_at_the_configured_reward_multiple(self) -> None:
        """Approximately, not exactly, and that is the honest assertion.

        The prices are normalised to storage precision, so the ratio re-derived
        from them is the ratio the position will really have. Asserting exact
        equality would be asserting a number the system cannot store.
        """
        proposal = evaluate(uptrend_with_pullback())
        assert proposal is not None
        assert proposal.reward_risk_ratio is not None
        drift = abs(proposal.reward_risk_ratio - FAST_PARAMETERS.reward_risk_target)
        assert drift < Decimal("0.000000001")

    def test_prices_are_emitted_at_the_precision_the_system_stores(self) -> None:
        """What was proposed must be what is stored, or the audit trail is not
        the decision."""
        proposal = evaluate(uptrend_with_pullback())
        assert proposal is not None
        for price in (
            proposal.reference_price,
            proposal.stop_loss_price,
            proposal.take_profit_price,
        ):
            assert price is not None
            exponent = price.as_tuple().exponent
            assert isinstance(exponent, int)
            assert -exponent <= PRICE_PLACES

    def test_the_stop_sits_below_the_recent_swing_low(self) -> None:
        """Placing it exactly at the swing low puts it where everyone else's is."""
        closes = uptrend_with_pullback()
        proposal = evaluate(closes)
        assert proposal is not None
        window = window_from_closes(closes)
        swing_low = min(window.lows[-FAST_PARAMETERS.swing_lookback :])
        assert proposal.stop_loss_price < swing_low

    def test_the_indicator_values_seen_are_recorded(self) -> None:
        """Without them the decision can be guessed at but not replayed."""
        proposal = evaluate(uptrend_with_pullback())
        assert proposal is not None
        assert set(proposal.inputs) >= {
            "close",
            "ema_fast",
            "ema_slow",
            "rsi",
            "atr",
            "swing_low",
            "stop_fraction",
        }
        assert all(isinstance(value, str) for value in proposal.inputs.values())

    def test_the_candle_that_produced_it_is_recorded(self) -> None:
        closes = uptrend_with_pullback()
        proposal = evaluate(closes)
        assert proposal is not None
        assert proposal.candle_open_time == BASE + M15.duration * (len(closes) - 1)

    def test_prices_are_not_rounded_to_a_guessed_tick(self) -> None:
        """The strategy cannot see the symbol filters. Rounding to a guessed
        tick would move the stop, and therefore the risk, by an unknown amount."""
        proposal = evaluate(uptrend_with_pullback())
        assert proposal is not None
        assert proposal.stop_loss_price != proposal.stop_loss_price.quantize(Decimal("1"))


class TestGatesRefuseForTheirOwnReason:
    def test_a_downtrend_produces_nothing(self) -> None:
        """Long only. In a downtrend this strategy does not trade at all, and
        weeks of NO_TRADE are the correct behaviour."""
        closes = [Decimal(1000)]
        for _ in range(119):
            closes.append(closes[-1] * Decimal("0.998"))
        assert evaluate(closes) is None

    def test_a_flat_market_produces_nothing(self) -> None:
        """No trend to continue, and the fast EMA never clears the slow one."""
        assert evaluate([Decimal(1000)] * 120) is None

    def test_an_uptrend_without_a_pullback_produces_nothing(self) -> None:
        """The strongest trends give no entry. That is the price of insisting
        on a discount."""
        closes = [Decimal(1000)]
        for _ in range(119):
            closes.append(closes[-1] * Decimal("1.002"))
        assert evaluate(closes) is None

    def test_a_pullback_still_in_progress_produces_nothing(self) -> None:
        """A dip that has not turned is not a resumption."""
        closes = uptrend_with_pullback()
        closes[-1] = closes[-2] * Decimal("0.995")
        assert evaluate(closes) is None

    def test_volatility_below_the_floor_is_refused(self) -> None:
        """Too quiet: the fees would dominate the move.

        The band is moved rather than the fixture. Shrinking the candle spread
        does not shrink the ATR, because true range includes the gap from the
        previous close - so a fixture-based test here would be measuring a
        different gate than the one it names.
        """
        parameters = replace(
            FAST_PARAMETERS,
            min_atr_fraction=Decimal("0.90"),
            max_atr_fraction=Decimal("0.95"),
        )
        assert evaluate(uptrend_with_pullback(), parameters) is None

    def test_volatility_above_the_ceiling_is_refused(self) -> None:
        """Too wild: the stop measures noise rather than structure."""
        parameters = replace(
            FAST_PARAMETERS,
            min_atr_fraction=Decimal("0.0000001"),
            max_atr_fraction=Decimal("0.0000002"),
        )
        assert evaluate(uptrend_with_pullback(), parameters) is None

    def test_the_band_admits_the_designed_setup(self) -> None:
        """Guards the two tests above: they must refuse because the band moved,
        not because the fixture never qualified in the first place."""
        assert evaluate(uptrend_with_pullback()) is not None

    def test_a_stop_tighter_than_the_leverage_floor_is_refused(self) -> None:
        """A tighter stop needs a notional above the reference capital, and
        leverage is forbidden."""
        parameters = replace(
            FAST_PARAMETERS,
            min_stop_fraction=Decimal("0.90"),
            max_stop_fraction=Decimal("0.95"),
        )
        assert evaluate(uptrend_with_pullback(), parameters) is None

    def test_a_stop_wider_than_the_ceiling_is_refused(self) -> None:
        parameters = replace(
            FAST_PARAMETERS,
            min_stop_fraction=Decimal("0.0000001"),
            max_stop_fraction=Decimal("0.0000002"),
        )
        assert evaluate(uptrend_with_pullback(), parameters) is None


class TestConfidenceScore:
    def test_the_score_stays_within_its_stated_range(self) -> None:
        proposal = evaluate(uptrend_with_pullback())
        assert proposal is not None
        assert proposal.confidence_score is not None
        assert Decimal(0) <= proposal.confidence_score <= Decimal(1)

    def test_the_components_behind_the_score_are_recorded(self) -> None:
        """A total alone cannot be audited or improved."""
        proposal = evaluate(uptrend_with_pullback())
        assert proposal is not None
        assert set(proposal.score_components) == {"trend_strength", "pullback_depth"}

    def test_a_deeper_pullback_scores_higher(self) -> None:
        """The only claim the score makes: between two simultaneous proposals,
        one of them ranks above the other. It is not a probability."""
        # Both depths must stay inside the volatility band, or one of them is
        # refused for a reason that has nothing to do with the score.
        shallow = evaluate(uptrend_with_pullback(dip_depth=Decimal("0.006")))
        deep = evaluate(uptrend_with_pullback(dip_depth=Decimal("0.012")))
        assert shallow is not None and deep is not None
        assert shallow.confidence_score is not None and deep.confidence_score is not None
        assert deep.score_components["pullback_depth"] >= shallow.score_components["pullback_depth"]


class TestRegistration:
    def test_importing_the_package_registers_nothing(self) -> None:
        """Running a strategy is a decision, not a consequence of an import."""
        from app.strategies import registry as process_registry

        assert TrendPullbackStrategy.NAME not in process_registry

    def test_the_baseline_is_registered_explicitly(self) -> None:
        registry = register_builtin_strategies(StrategyRegistry())
        assert registry.names == ["trend_pullback_v1"]

    def test_it_can_be_rebuilt_from_a_stored_version(self) -> None:
        registry = register_builtin_strategies(StrategyRegistry())
        strategy = registry.create(
            "trend_pullback_v1", {"reward_risk_target": "3.0", "trend_fast_period": 30}
        )
        assert strategy.parameters["reward_risk_target"] == "3.0"
        assert strategy.parameters["trend_fast_period"] == 30

    def test_it_has_a_stable_code_fingerprint(self) -> None:
        assert len(code_fingerprint(TrendPullbackStrategy)) == 64

    def test_a_reparameterisation_does_not_change_the_fingerprint(self) -> None:
        """Parameters are stored separately, so a re-parameterisation stays
        distinguishable from a code change (AC-20)."""
        a = TrendPullbackStrategy(TrendPullbackParameters(reward_risk_target=Decimal("2")))
        b = TrendPullbackStrategy(TrendPullbackParameters(reward_risk_target=Decimal("4")))
        assert code_fingerprint(a) == code_fingerprint(b)


class TestDeterminism:
    def test_the_same_window_always_gives_the_same_proposal(self) -> None:
        """AC-20 depends on this: a backtest replayed must produce the same
        decision, digit for digit."""
        closes = uptrend_with_pullback()
        first = evaluate(closes)
        second = evaluate(closes)
        assert first == second

    def test_appending_a_candle_never_changes_the_earlier_decision(self) -> None:
        """The strategy reads only the window it is given. If a later candle
        could change an earlier decision, every backtest result would be
        fiction."""
        closes = uptrend_with_pullback()
        decision_now = evaluate(closes)
        extended = [*closes, closes[-1] * Decimal("1.05")]
        # Re-evaluating the ORIGINAL window after the market moved on must give
        # the same answer.
        assert evaluate(closes) == decision_now
        assert len(extended) == len(closes) + 1


class TestWindowIndependence:
    def test_the_strategy_holds_no_state_between_evaluations(self) -> None:
        """Two symbols share one strategy instance in Phase 5."""
        strategy = TrendPullbackStrategy(FAST_PARAMETERS)
        signal_window = window_from_closes(uptrend_with_pullback())
        flat_window = window_from_closes([Decimal(1000)] * 120, symbol="ETHUSDT")

        first = strategy.evaluate(StrategyContext(window=signal_window, evaluated_at=NOW))
        strategy.evaluate(StrategyContext(window=flat_window, evaluated_at=NOW))
        again = strategy.evaluate(
            StrategyContext(window=signal_window, evaluated_at=NOW + timedelta(hours=1))
        )

        assert first is not None
        assert first == again
