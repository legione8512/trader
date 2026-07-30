"""The strategy contract.

These tests are mostly about what a strategy is *unable* to do. The framework's
value is not in what it enables but in what it forecloses, so that is what is
asserted.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.domain.candle_window import CandleWindow
from app.domain.enums import OrderSide, Timeframe
from app.strategies.base import (
    SignalProposal,
    Strategy,
    StrategyContext,
    StrategyError,
    code_fingerprint,
)
from app.strategies.registry import StrategyRegistry

M15 = Timeframe.M15
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def empty_context() -> StrategyContext:
    return StrategyContext(window=CandleWindow.empty("BTCUSDT", M15), evaluated_at=NOW)


@dataclass(frozen=True, slots=True)
class AlwaysLong:
    """A minimal strategy, used to exercise the contract."""

    threshold: Decimal = Decimal(100)

    @property
    def name(self) -> str:
        return "always_long"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"threshold": str(self.threshold)}

    @property
    def required_candles(self) -> int:
        return 20

    def evaluate(self, context: StrategyContext) -> SignalProposal | None:
        if not context.window.has_at_least(self.required_candles):
            return None
        return SignalProposal(
            side=OrderSide.BUY,
            reference_price=Decimal(100),
            stop_loss_price=Decimal(98),
        )


class TestProposalHasNoQuantity:
    """The single most important property of the contract.

    A strategy cannot express "buy 0.5 BTC". Sizing needs the risk budget, the
    daily loss so far and the exchange filters, none of which a strategy can
    see - so the field it would need does not exist.
    """

    def test_a_proposal_carries_no_quantity_field(self) -> None:
        fields = {field.name for field in SignalProposal.__dataclass_fields__.values()}
        forbidden = {"quantity", "proposed_quantity", "size", "position_size", "notional"}
        assert not (fields & forbidden), (
            f"SignalProposal exposes {fields & forbidden}. Sizing belongs to the risk engine."
        )

    def test_it_carries_the_stop_distance_the_risk_engine_needs(self) -> None:
        proposal = SignalProposal(
            side=OrderSide.BUY,
            reference_price=Decimal(100),
            stop_loss_price=Decimal(96),
        )
        assert proposal.stop_distance == Decimal(4)


class TestProposalValidation:
    def test_a_long_stop_above_the_entry_is_refused(self) -> None:
        """Not a strategy choice: an inverted sign. It would make 1R negative."""
        with pytest.raises(StrategyError, match="long stop must be below"):
            SignalProposal(
                side=OrderSide.BUY,
                reference_price=Decimal(100),
                stop_loss_price=Decimal(102),
            )

    def test_a_long_target_below_the_entry_is_refused(self) -> None:
        with pytest.raises(StrategyError, match="long target must be above"):
            SignalProposal(
                side=OrderSide.BUY,
                reference_price=Decimal(100),
                stop_loss_price=Decimal(98),
                take_profit_price=Decimal(99),
            )

    def test_a_short_stop_below_the_entry_is_refused(self) -> None:
        with pytest.raises(StrategyError, match="short stop must be above"):
            SignalProposal(
                side=OrderSide.SELL,
                reference_price=Decimal(100),
                stop_loss_price=Decimal(98),
            )

    def test_a_short_target_above_the_entry_is_refused(self) -> None:
        with pytest.raises(StrategyError, match="short target must be below"):
            SignalProposal(
                side=OrderSide.SELL,
                reference_price=Decimal(100),
                stop_loss_price=Decimal(102),
                take_profit_price=Decimal(101),
            )

    def test_a_non_positive_price_is_refused(self) -> None:
        with pytest.raises(StrategyError, match="Reference price must be positive"):
            SignalProposal(
                side=OrderSide.BUY,
                reference_price=Decimal(0),
                stop_loss_price=Decimal(-1),
            )

    def test_a_valid_short_is_accepted(self) -> None:
        proposal = SignalProposal(
            side=OrderSide.SELL,
            reference_price=Decimal(100),
            stop_loss_price=Decimal(104),
            take_profit_price=Decimal(92),
        )
        assert proposal.stop_distance == Decimal(4)
        assert proposal.reward_risk_ratio == Decimal(2)

    def test_the_reward_risk_ratio_is_gross_and_absent_without_a_target(self) -> None:
        """Named gross on purpose: the ratio that decides anything (R-14) is net
        of fees and slippage, which only the risk engine knows."""
        proposal = SignalProposal(
            side=OrderSide.BUY,
            reference_price=Decimal(100),
            stop_loss_price=Decimal(98),
        )
        assert proposal.reward_risk_ratio is None

    def test_a_proposal_is_immutable(self) -> None:
        proposal = SignalProposal(
            side=OrderSide.BUY,
            reference_price=Decimal(100),
            stop_loss_price=Decimal(98),
        )
        with pytest.raises(AttributeError):
            proposal.reference_price = Decimal(1)  # type: ignore[misc]


class TestContext:
    def test_the_context_carries_no_balance_or_position(self) -> None:
        """A strategy that cannot see the balance cannot size against it, and
        one that cannot see the open position cannot decide to add to it."""
        fields = {field.name for field in StrategyContext.__dataclass_fields__.values()}
        forbidden = {
            "balance",
            "balances",
            "position",
            "positions",
            "open_orders",
            "adapter",
            "exchange",
            "session",
            "repository",
        }
        assert not (fields & forbidden), f"StrategyContext exposes {fields & forbidden}"

    def test_the_evaluation_time_is_injected_not_read(self) -> None:
        """A backtest and a live run must take the same code path."""
        context = StrategyContext(window=CandleWindow.empty("BTCUSDT", M15), evaluated_at=NOW)
        assert context.evaluated_at == NOW

    def test_a_naive_evaluation_time_is_refused(self) -> None:
        with pytest.raises(StrategyError, match="timezone-aware"):
            StrategyContext(
                window=CandleWindow.empty("BTCUSDT", M15),
                evaluated_at=datetime(2026, 7, 28, 12, 0),  # noqa: DTZ001
            )

    def test_the_symbol_and_timeframe_come_from_the_window(self) -> None:
        context = empty_context()
        assert context.symbol == "BTCUSDT"
        assert context.timeframe is M15


class TestEvaluationIsSynchronous:
    def test_the_protocol_method_is_not_a_coroutine(self) -> None:
        """Every I/O path here is async. A synchronous evaluate cannot await the
        exchange client, a repository or a session, so the signature removes the
        capability rather than discouraging its use."""
        assert not inspect.iscoroutinefunction(Strategy.evaluate)

    def test_no_signal_is_a_valid_answer(self) -> None:
        """The system is specified to refuse to trade when nothing qualifies.
        "No signal" is an answer, not a failure."""
        assert AlwaysLong().evaluate(empty_context()) is None

    def test_a_strategy_declares_its_own_warm_up_requirement(self) -> None:
        assert AlwaysLong().required_candles == 20


class TestCodeFingerprint:
    def test_the_same_code_gives_the_same_fingerprint(self) -> None:
        assert code_fingerprint(AlwaysLong()) == code_fingerprint(AlwaysLong())

    def test_an_instance_and_its_class_agree(self) -> None:
        assert code_fingerprint(AlwaysLong()) == code_fingerprint(AlwaysLong)

    def test_different_code_gives_a_different_fingerprint(self) -> None:
        """Two runs with the same parameters but different code are not the
        same experiment (AC-20)."""

        @dataclass(frozen=True, slots=True)
        class AlwaysShort:
            @property
            def name(self) -> str:
                return "always_short"

            @property
            def parameters(self) -> dict[str, Any]:
                return {}

            @property
            def required_candles(self) -> int:
                return 20

            def evaluate(self, context: StrategyContext) -> SignalProposal | None:
                return None

        assert code_fingerprint(AlwaysShort) != code_fingerprint(AlwaysLong)

    def test_the_fingerprint_fits_the_stored_column(self) -> None:
        """strategy_version.code_fingerprint is VARCHAR(64); SHA-256 hex is 64."""
        assert len(code_fingerprint(AlwaysLong)) == 64

    def test_parameters_do_not_change_the_fingerprint(self) -> None:
        """Parameters are stored separately. Mixing them into the fingerprint
        would make it impossible to tell a re-parameterisation from a code
        change."""
        assert code_fingerprint(AlwaysLong(threshold=Decimal(1))) == code_fingerprint(
            AlwaysLong(threshold=Decimal(999))
        )


class TestRegistry:
    def test_a_registered_strategy_can_be_created_from_stored_parameters(self) -> None:
        registry = StrategyRegistry()
        registry.register("always_long", lambda params: AlwaysLong(**params))

        strategy = registry.create("always_long", {"threshold": Decimal(50)})

        assert strategy.name == "always_long"
        assert strategy.parameters == {"threshold": "50"}

    def test_a_duplicate_name_is_refused_not_overwritten(self) -> None:
        """Two strategies sharing a name make every stored signal ambiguous
        about which one produced it."""
        registry = StrategyRegistry()
        registry.register("always_long", lambda params: AlwaysLong())
        with pytest.raises(StrategyError, match="already registered"):
            registry.register("always_long", lambda params: AlwaysLong())

    def test_an_unknown_name_is_refused_with_the_known_ones(self) -> None:
        registry = StrategyRegistry()
        registry.register("always_long", lambda params: AlwaysLong())
        with pytest.raises(StrategyError, match="always_long"):
            registry.create("missing")

    def test_a_strategy_that_disagrees_about_its_own_name_is_refused(self) -> None:
        """Otherwise a signal is filed under one name and replayed under another."""
        registry = StrategyRegistry()
        registry.register("misnamed", lambda params: AlwaysLong())
        with pytest.raises(StrategyError, match="reports its name"):
            registry.create("misnamed")

    def test_an_empty_name_is_refused(self) -> None:
        registry = StrategyRegistry()
        with pytest.raises(StrategyError, match="cannot be empty"):
            registry.register("", lambda params: AlwaysLong())

    def test_the_registry_reports_what_it_knows(self) -> None:
        registry = StrategyRegistry()
        registry.register("always_long", lambda params: AlwaysLong())
        assert registry.names == ["always_long"]
        assert "always_long" in registry
        assert len(registry) == 1

    def test_the_registry_exposes_a_fingerprint_per_strategy(self) -> None:
        registry = StrategyRegistry()
        registry.register("always_long", lambda params: AlwaysLong())
        assert registry.fingerprint("always_long") == code_fingerprint(AlwaysLong)

    def test_nothing_is_registered_before_a_design_is_reviewed(self) -> None:
        """The baseline strategy is registered in 4.3, after its design has been
        explained and approved - not by importing a module."""
        from app.strategies import registry as process_registry

        assert process_registry.names == []
