"""State machines, expressed as data rather than scattered conditionals.

Allowed transitions live in a mapping. That makes it possible to assert
*properties of the whole graph* in tests - every state reachable, terminal
states have no outgoing edges, no edge points at a state outside the enum.
With ``if`` statements spread across services, none of that is checkable.

Self-transitions are deliberately not allowed. A session that evaluates an
opportunity and decides ``NO_TRADE`` has not changed state, so it performs no
transition at all.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from app.domain.enums import (
    HealthStatus,
    OrderStatus,
    PositionStatus,
    SignalStatus,
    TradingDayStatus,
    TradingSessionStatus,
)
from app.domain.errors import InvalidTransitionError


@dataclass(frozen=True, slots=True)
class StateMachine[S: StrEnum]:
    """A directed graph of allowed state transitions."""

    name: str
    initial: S
    transitions: Mapping[S, frozenset[S]]

    def allowed_from(self, current: S) -> frozenset[S]:
        return self.transitions.get(current, frozenset())

    def can_transition(self, current: S, target: S) -> bool:
        return target in self.allowed_from(current)

    def assert_transition(self, current: S, target: S) -> None:
        """Raise unless the transition is allowed."""
        if not self.can_transition(current, target):
            allowed = tuple(sorted(state.value for state in self.allowed_from(current)))
            raise InvalidTransitionError(self.name, current.value, target.value, allowed)

    def is_terminal(self, state: S) -> bool:
        return not self.allowed_from(state)

    @property
    def states(self) -> frozenset[S]:
        reachable: set[S] = set(self.transitions)
        for targets in self.transitions.values():
            reachable |= targets
        reachable.add(self.initial)
        return frozenset(reachable)

    @property
    def terminal_states(self) -> frozenset[S]:
        return frozenset(state for state in self.states if self.is_terminal(state))


# ---------------------------------------------------------------------------
# Trading day
# ---------------------------------------------------------------------------

TRADING_DAY_MACHINE: Final = StateMachine[TradingDayStatus](
    name="TradingDay",
    initial=TradingDayStatus.PENDING,
    transitions={
        TradingDayStatus.PENDING: frozenset(
            {TradingDayStatus.ACTIVE, TradingDayStatus.TECHNICAL_FAILURE}
        ),
        TradingDayStatus.ACTIVE: frozenset(
            {
                TradingDayStatus.DAILY_TARGET_REACHED,
                TradingDayStatus.DAILY_STOP_REACHED,
                TradingDayStatus.TRADING_SUSPENDED,
                TradingDayStatus.MANUALLY_STOPPED,
                TradingDayStatus.TECHNICAL_FAILURE,
                # A day that simply ends with no trades: the NO_TRADE outcome.
                TradingDayStatus.CLOSED,
            }
        ),
        TradingDayStatus.TRADING_SUSPENDED: frozenset(
            {
                TradingDayStatus.ACTIVE,
                TradingDayStatus.MANUALLY_STOPPED,
                TradingDayStatus.TECHNICAL_FAILURE,
                TradingDayStatus.CLOSED,
            }
        ),
        TradingDayStatus.DAILY_TARGET_REACHED: frozenset({TradingDayStatus.CLOSED}),
        TradingDayStatus.DAILY_STOP_REACHED: frozenset({TradingDayStatus.CLOSED}),
        TradingDayStatus.MANUALLY_STOPPED: frozenset({TradingDayStatus.CLOSED}),
        TradingDayStatus.TECHNICAL_FAILURE: frozenset({TradingDayStatus.CLOSED}),
        TradingDayStatus.CLOSED: frozenset(),
    },
)


# ---------------------------------------------------------------------------
# Trading session
# ---------------------------------------------------------------------------

TRADING_SESSION_MACHINE: Final = StateMachine[TradingSessionStatus](
    name="TradingSession",
    initial=TradingSessionStatus.EVALUATING,
    transitions={
        TradingSessionStatus.EVALUATING: frozenset(
            {TradingSessionStatus.OPEN, TradingSessionStatus.ABORTED}
        ),
        TradingSessionStatus.OPEN: frozenset({TradingSessionStatus.CLOSING}),
        TradingSessionStatus.CLOSING: frozenset(
            {
                TradingSessionStatus.CLOSED_TARGET_REACHED,
                TradingSessionStatus.CLOSED_RESTART_ELIGIBLE,
                TradingSessionStatus.CLOSED_STOPPED,
                TradingSessionStatus.CLOSED_NO_OPPORTUNITY,
            }
        ),
        TradingSessionStatus.CLOSED_TARGET_REACHED: frozenset(),
        TradingSessionStatus.CLOSED_RESTART_ELIGIBLE: frozenset(),
        TradingSessionStatus.CLOSED_STOPPED: frozenset(),
        TradingSessionStatus.CLOSED_NO_OPPORTUNITY: frozenset(),
        TradingSessionStatus.ABORTED: frozenset(),
    },
)


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

SIGNAL_MACHINE: Final = StateMachine[SignalStatus](
    name="Signal",
    initial=SignalStatus.GENERATED,
    transitions={
        SignalStatus.GENERATED: frozenset({SignalStatus.RISK_APPROVED, SignalStatus.RISK_REJECTED}),
        SignalStatus.RISK_APPROVED: frozenset(
            {
                # SIGNAL_ONLY mode waits for the operator.
                SignalStatus.AWAITING_OPERATOR,
                # Automatic modes proceed directly.
                SignalStatus.ACCEPTED,
                SignalStatus.EXPIRED,
            }
        ),
        SignalStatus.AWAITING_OPERATOR: frozenset(
            {SignalStatus.ACCEPTED, SignalStatus.OPERATOR_REJECTED, SignalStatus.EXPIRED}
        ),
        SignalStatus.ACCEPTED: frozenset({SignalStatus.EXECUTED, SignalStatus.EXECUTION_FAILED}),
        SignalStatus.RISK_REJECTED: frozenset(),
        SignalStatus.OPERATOR_REJECTED: frozenset(),
        SignalStatus.EXPIRED: frozenset(),
        SignalStatus.EXECUTED: frozenset(),
        SignalStatus.EXECUTION_FAILED: frozenset(),
    },
)


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------

ORDER_MACHINE: Final = StateMachine[OrderStatus](
    name="Order",
    initial=OrderStatus.INTENT_RECORDED,
    transitions={
        OrderStatus.INTENT_RECORDED: frozenset({OrderStatus.SUBMITTING}),
        OrderStatus.SUBMITTING: frozenset(
            {OrderStatus.ACCEPTED, OrderStatus.REJECTED, OrderStatus.UNKNOWN}
        ),
        # The single most important edge in the system. A timeout NEVER leads
        # to a blind retry; it leads to asking the exchange what actually
        # happened.
        OrderStatus.UNKNOWN: frozenset({OrderStatus.RECONCILING}),
        OrderStatus.RECONCILING: frozenset(
            {
                OrderStatus.ACCEPTED,
                OrderStatus.REJECTED,
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.EXPIRED,
                # Reconciliation itself failed: escalate and suspend trading.
                OrderStatus.UNRESOLVED,
            }
        ),
        OrderStatus.ACCEPTED: frozenset(
            {
                OrderStatus.PARTIALLY_FILLED,
                OrderStatus.FILLED,
                OrderStatus.CANCELED,
                OrderStatus.EXPIRED,
                # A cancel or amend request can time out just like a submit.
                OrderStatus.UNKNOWN,
            }
        ),
        OrderStatus.PARTIALLY_FILLED: frozenset(
            {OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.EXPIRED, OrderStatus.UNKNOWN}
        ),
        OrderStatus.FILLED: frozenset(),
        OrderStatus.CANCELED: frozenset(),
        OrderStatus.EXPIRED: frozenset(),
        OrderStatus.REJECTED: frozenset(),
        OrderStatus.UNRESOLVED: frozenset(),
    },
)


# ---------------------------------------------------------------------------
# Position
# ---------------------------------------------------------------------------

POSITION_MACHINE: Final = StateMachine[PositionStatus](
    name="Position",
    initial=PositionStatus.OPENING,
    transitions={
        PositionStatus.OPENING: frozenset({PositionStatus.OPEN, PositionStatus.ABANDONED}),
        PositionStatus.OPEN: frozenset({PositionStatus.CLOSING, PositionStatus.DESYNCED}),
        PositionStatus.CLOSING: frozenset({PositionStatus.CLOSED, PositionStatus.DESYNCED}),
        PositionStatus.DESYNCED: frozenset({PositionStatus.OPEN, PositionStatus.CLOSED}),
        PositionStatus.CLOSED: frozenset(),
        PositionStatus.ABANDONED: frozenset(),
    },
)


# ---------------------------------------------------------------------------
# System health
# ---------------------------------------------------------------------------

SYSTEM_HEALTH_MACHINE: Final = StateMachine[HealthStatus](
    name="SystemHealth",
    initial=HealthStatus.STARTING,
    transitions={
        HealthStatus.STARTING: frozenset({HealthStatus.HEALTHY, HealthStatus.UNHEALTHY}),
        HealthStatus.HEALTHY: frozenset(
            # A hard failure, such as the database disappearing, goes straight
            # to UNHEALTHY. Forcing it through DEGRADED would misreport an
            # outage as a warning for one cycle.
            {HealthStatus.DEGRADED, HealthStatus.UNHEALTHY}
        ),
        HealthStatus.DEGRADED: frozenset({HealthStatus.HEALTHY, HealthStatus.UNHEALTHY}),
        HealthStatus.UNHEALTHY: frozenset({HealthStatus.DEGRADED, HealthStatus.HEALTHY}),
    },
)


ALL_MACHINES: Final = (
    TRADING_DAY_MACHINE,
    TRADING_SESSION_MACHINE,
    SIGNAL_MACHINE,
    ORDER_MACHINE,
    POSITION_MACHINE,
    SYSTEM_HEALTH_MACHINE,
)
