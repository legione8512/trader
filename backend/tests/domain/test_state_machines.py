"""State machine tests.

The graph properties apply to every machine at once. Adding a seventh machine
to ALL_MACHINES automatically subjects it to the same rules.
"""

from __future__ import annotations

from enum import StrEnum

import pytest

from app.domain.enums import (
    HealthStatus,
    OrderStatus,
    PositionStatus,
    SignalStatus,
    TradingDayStatus,
    TradingSessionStatus,
)
from app.domain.errors import InvalidTransitionError
from app.domain.state_machines import (
    ALL_MACHINES,
    ORDER_MACHINE,
    POSITION_MACHINE,
    SIGNAL_MACHINE,
    SYSTEM_HEALTH_MACHINE,
    TRADING_DAY_MACHINE,
    TRADING_SESSION_MACHINE,
    StateMachine,
)


def reachable_states[S: StrEnum](machine: StateMachine[S]) -> set[S]:
    """Breadth-first walk from the initial state."""
    seen: set[S] = {machine.initial}
    frontier = [machine.initial]
    while frontier:
        current = frontier.pop()
        for target in machine.allowed_from(current):
            if target not in seen:
                seen.add(target)
                frontier.append(target)
    return seen


class TestGraphProperties:
    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_every_state_is_reachable_from_the_initial_state(
        self, machine: StateMachine[StrEnum]
    ) -> None:
        """An unreachable state is dead code that will drift out of sync."""
        assert reachable_states(machine) == machine.states

    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_no_self_transitions(self, machine: StateMachine[StrEnum]) -> None:
        """Staying in a state is not a transition and must not be modelled as one."""
        for state, targets in machine.transitions.items():
            assert state not in targets, f"{machine.name}: {state} transitions to itself"

    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_every_state_appears_as_a_key(self, machine: StateMachine[StrEnum]) -> None:
        """Terminal states must be declared explicitly, with an empty set."""
        assert machine.states == frozenset(machine.transitions)

    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_terminal_states_have_no_outgoing_edges(self, machine: StateMachine[StrEnum]) -> None:
        for state in machine.terminal_states:
            assert machine.allowed_from(state) == frozenset()

    @pytest.mark.parametrize("machine", ALL_MACHINES, ids=lambda m: m.name)
    def test_an_illegal_transition_raises_and_lists_what_is_allowed(
        self, machine: StateMachine[StrEnum]
    ) -> None:
        illegal = next(
            (
                (source, target)
                for source in sorted(machine.states, key=lambda state: state.value)
                for target in sorted(machine.states, key=lambda state: state.value)
                if source is not target and not machine.can_transition(source, target)
            ),
            None,
        )
        assert illegal is not None, f"{machine.name} allows every transition"
        source, target = illegal

        with pytest.raises(InvalidTransitionError) as caught:
            machine.assert_transition(source, target)

        error = caught.value
        assert error.machine == machine.name
        assert error.current == source.value
        assert error.target == target.value
        assert error.allowed == tuple(sorted(state.value for state in machine.allowed_from(source)))


class TestOrderMachine:
    """The order machine is where a bug costs real money."""

    def test_an_intent_is_recorded_before_submission(self) -> None:
        assert ORDER_MACHINE.initial is OrderStatus.INTENT_RECORDED
        assert ORDER_MACHINE.can_transition(OrderStatus.INTENT_RECORDED, OrderStatus.SUBMITTING)

    def test_a_timeout_can_only_lead_to_reconciliation(self) -> None:
        """AC-15: an uncertain order triggers reconciliation, never a retry."""
        assert ORDER_MACHINE.allowed_from(OrderStatus.UNKNOWN) == frozenset(
            {OrderStatus.RECONCILING}
        )

    def test_a_blind_retry_is_structurally_impossible(self) -> None:
        """This is the transition that makes bots resubmit an accepted order."""
        assert not ORDER_MACHINE.can_transition(OrderStatus.UNKNOWN, OrderStatus.SUBMITTING)
        with pytest.raises(InvalidTransitionError):
            ORDER_MACHINE.assert_transition(OrderStatus.UNKNOWN, OrderStatus.SUBMITTING)

    def test_reconciliation_can_discover_any_real_outcome(self) -> None:
        outcomes = ORDER_MACHINE.allowed_from(OrderStatus.RECONCILING)
        assert {
            OrderStatus.ACCEPTED,
            OrderStatus.REJECTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.UNRESOLVED,
        } == outcomes

    def test_a_cancel_can_also_time_out(self) -> None:
        assert ORDER_MACHINE.can_transition(OrderStatus.ACCEPTED, OrderStatus.UNKNOWN)
        assert ORDER_MACHINE.can_transition(OrderStatus.PARTIALLY_FILLED, OrderStatus.UNKNOWN)

    def test_a_filled_order_is_final(self) -> None:
        assert ORDER_MACHINE.is_terminal(OrderStatus.FILLED)

    def test_unresolved_is_terminal_and_requires_escalation(self) -> None:
        assert ORDER_MACHINE.is_terminal(OrderStatus.UNRESOLVED)


class TestTradingDayMachine:
    def test_a_day_starts_pending(self) -> None:
        assert TRADING_DAY_MACHINE.initial is TradingDayStatus.PENDING

    def test_a_day_with_no_trades_can_close_normally(self) -> None:
        """AC-01: zero trades is a valid outcome, not an error path."""
        assert TRADING_DAY_MACHINE.can_transition(TradingDayStatus.ACTIVE, TradingDayStatus.CLOSED)

    def test_a_suspended_day_can_recover(self) -> None:
        assert TRADING_DAY_MACHINE.can_transition(
            TradingDayStatus.TRADING_SUSPENDED, TradingDayStatus.ACTIVE
        )

    def test_a_stopped_day_can_never_return_to_active(self) -> None:
        """AC-05: a daily stop is final for that day. No restart bypasses it."""
        for stopped in (
            TradingDayStatus.DAILY_STOP_REACHED,
            TradingDayStatus.DAILY_TARGET_REACHED,
            TradingDayStatus.MANUALLY_STOPPED,
        ):
            assert not TRADING_DAY_MACHINE.can_transition(stopped, TradingDayStatus.ACTIVE)
            assert TRADING_DAY_MACHINE.allowed_from(stopped) == frozenset({TradingDayStatus.CLOSED})

    def test_only_an_active_day_allows_new_entries(self) -> None:
        for status in TradingDayStatus:
            assert status.allows_new_entries is (status is TradingDayStatus.ACTIVE)


class TestTradingSessionMachine:
    def test_a_session_starts_by_evaluating(self) -> None:
        assert TRADING_SESSION_MACHINE.initial is TradingSessionStatus.EVALUATING

    def test_a_session_can_abort_before_any_trade(self) -> None:
        assert TRADING_SESSION_MACHINE.can_transition(
            TradingSessionStatus.EVALUATING, TradingSessionStatus.ABORTED
        )

    def test_restart_eligibility_is_terminal(self) -> None:
        """CLOSED_RESTART_ELIGIBLE makes a new session possible, never automatic."""
        assert TRADING_SESSION_MACHINE.is_terminal(TradingSessionStatus.CLOSED_RESTART_ELIGIBLE)

    def test_only_the_restart_eligible_close_permits_re_evaluation(self) -> None:
        for status in TradingSessionStatus:
            expected = status is TradingSessionStatus.CLOSED_RESTART_ELIGIBLE
            assert status.makes_restart_possible is expected


class TestSignalMachine:
    def test_a_signal_must_pass_risk_before_anything_else(self) -> None:
        """A strategy proposal can only go to the risk engine first."""
        assert SIGNAL_MACHINE.allowed_from(SignalStatus.GENERATED) == frozenset(
            {SignalStatus.RISK_APPROVED, SignalStatus.RISK_REJECTED}
        )

    def test_a_generated_signal_can_never_jump_straight_to_execution(self) -> None:
        for target in (SignalStatus.ACCEPTED, SignalStatus.EXECUTED):
            assert not SIGNAL_MACHINE.can_transition(SignalStatus.GENERATED, target)

    def test_a_rejected_signal_is_final(self) -> None:
        assert SIGNAL_MACHINE.is_terminal(SignalStatus.RISK_REJECTED)

    def test_an_approved_signal_can_expire_before_the_operator_answers(self) -> None:
        assert SIGNAL_MACHINE.can_transition(SignalStatus.AWAITING_OPERATOR, SignalStatus.EXPIRED)


class TestPositionMachine:
    def test_a_desynced_position_can_be_reconciled_either_way(self) -> None:
        assert POSITION_MACHINE.allowed_from(PositionStatus.DESYNCED) == frozenset(
            {PositionStatus.OPEN, PositionStatus.CLOSED}
        )

    def test_position_slot_occupancy(self) -> None:
        """A carried or desynced position still consumes the single slot."""
        occupying = {status for status in PositionStatus if status.occupies_a_position_slot}
        assert occupying == {
            PositionStatus.OPENING,
            PositionStatus.OPEN,
            PositionStatus.CLOSING,
            PositionStatus.DESYNCED,
        }


class TestSystemHealthMachine:
    def test_a_hard_failure_goes_straight_to_unhealthy(self) -> None:
        assert SYSTEM_HEALTH_MACHINE.can_transition(HealthStatus.HEALTHY, HealthStatus.UNHEALTHY)

    def test_recovery_is_possible_from_every_bad_state(self) -> None:
        for status in (HealthStatus.DEGRADED, HealthStatus.UNHEALTHY):
            assert SYSTEM_HEALTH_MACHINE.can_transition(status, HealthStatus.HEALTHY)

    def test_health_never_reaches_a_terminal_state(self) -> None:
        """A running system can always change health. Nothing here is final."""
        assert SYSTEM_HEALTH_MACHINE.terminal_states == frozenset()

    def test_only_healthy_allows_new_positions(self) -> None:
        for status in HealthStatus:
            assert status.blocks_new_positions is (status is not HealthStatus.HEALTHY)
