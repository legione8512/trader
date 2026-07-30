"""Core domain enumerations.

This module is part of the pure domain layer: it performs no I/O and imports
nothing from the infrastructure layers.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from enum import StrEnum


class AppEnvironment(StrEnum):
    """Deployment environment the application is running in."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class AutonomyMode(StrEnum):
    """How much authority the application has to act on its own decisions.

    The three modes are mutually exclusive. See docs/SRS.md section 7.
    """

    #: Analyse and propose. Never submits an exchange order.
    SIGNAL_ONLY = "SIGNAL_ONLY"

    #: Decide and execute automatically against the paper adapter only.
    PAPER_AUTOMATIC = "PAPER_AUTOMATIC"

    #: Real money on a real exchange. Disabled by default, four guards required.
    LIVE_AUTOMATIC = "LIVE_AUTOMATIC"

    @property
    def submits_real_orders(self) -> bool:
        return self is AutonomyMode.LIVE_AUTOMATIC


class HealthStatus(StrEnum):
    """System health state. See docs/STATE_MACHINES.md section 6.

    ``DEGRADED`` and ``UNHEALTHY`` both block opening new positions. Management
    of existing positions continues in every state: abandoning an open position
    is more dangerous than declining to open a new one.
    """

    STARTING = "STARTING"
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    UNHEALTHY = "UNHEALTHY"

    @property
    def blocks_new_positions(self) -> bool:
        """Whether this state forbids opening new positions."""
        return self is not HealthStatus.HEALTHY

    @classmethod
    def worst(cls, statuses: Iterable[HealthStatus]) -> HealthStatus:
        """Aggregate several statuses into the most severe one.

        An empty input is treated as ``STARTING`` rather than ``HEALTHY``:
        "no checks ran" must never be reported as "everything is fine".
        """
        severity: dict[HealthStatus, int] = {
            cls.HEALTHY: 0,
            cls.STARTING: 1,
            cls.DEGRADED: 2,
            cls.UNHEALTHY: 3,
        }
        collected = list(statuses)
        if not collected:
            return cls.STARTING
        return max(collected, key=lambda status: severity[status])


class TradingDayStatus(StrEnum):
    """State of a calendar trading day. See docs/STATE_MACHINES.md section 1."""

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    TRADING_SUSPENDED = "TRADING_SUSPENDED"
    DAILY_TARGET_REACHED = "DAILY_TARGET_REACHED"
    DAILY_STOP_REACHED = "DAILY_STOP_REACHED"
    MANUALLY_STOPPED = "MANUALLY_STOPPED"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"
    CLOSED = "CLOSED"

    @property
    def allows_new_entries(self) -> bool:
        """Only an ACTIVE day may open new positions.

        Every halted state still manages existing positions to exit.
        """
        return self is TradingDayStatus.ACTIVE


class TradingSessionStatus(StrEnum):
    """State of a trading session. See docs/STATE_MACHINES.md section 2."""

    EVALUATING = "EVALUATING"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED_TARGET_REACHED = "CLOSED_TARGET_REACHED"
    CLOSED_RESTART_ELIGIBLE = "CLOSED_RESTART_ELIGIBLE"
    CLOSED_STOPPED = "CLOSED_STOPPED"
    CLOSED_NO_OPPORTUNITY = "CLOSED_NO_OPPORTUNITY"
    ABORTED = "ABORTED"

    @property
    def makes_restart_possible(self) -> bool:
        """Whether the day MAY evaluate a new session afterwards.

        Possible, never automatic. A new session starts only when a fresh
        opportunity independently satisfies every strategy and risk criterion.
        """
        return self is TradingSessionStatus.CLOSED_RESTART_ELIGIBLE


class SignalStatus(StrEnum):
    """State of a generated signal. See docs/STATE_MACHINES.md section 3."""

    GENERATED = "GENERATED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    AWAITING_OPERATOR = "AWAITING_OPERATOR"
    OPERATOR_REJECTED = "OPERATOR_REJECTED"
    ACCEPTED = "ACCEPTED"
    EXECUTED = "EXECUTED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    EXPIRED = "EXPIRED"


class OrderStatus(StrEnum):
    """State of an order. See docs/STATE_MACHINES.md section 4."""

    #: Persisted before submission, so a crash mid-flight stays recoverable.
    INTENT_RECORDED = "INTENT_RECORDED"
    SUBMITTING = "SUBMITTING"
    ACCEPTED = "ACCEPTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    #: Timeout or network error. The exchange state is genuinely unknown.
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"
    #: Reconciliation failed. Escalate and suspend trading.
    UNRESOLVED = "UNRESOLVED"

    @property
    def is_open_on_exchange(self) -> bool:
        return self in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}

    @property
    def requires_reconciliation(self) -> bool:
        """Never retry blindly from this state. Ask the exchange first."""
        return self is OrderStatus.UNKNOWN


class PositionStatus(StrEnum):
    """State of a position. See docs/STATE_MACHINES.md section 5."""

    OPENING = "OPENING"
    OPEN = "OPEN"
    CLOSING = "CLOSING"
    CLOSED = "CLOSED"
    #: Local state disagrees with the exchange. Blocks all new entries.
    DESYNCED = "DESYNCED"
    ABANDONED = "ABANDONED"

    @property
    def occupies_a_position_slot(self) -> bool:
        """Whether this position counts against maximumOpenPositions."""
        return self in {
            PositionStatus.OPENING,
            PositionStatus.OPEN,
            PositionStatus.CLOSING,
            PositionStatus.DESYNCED,
        }


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(StrEnum):
    GTC = "GTC"
    IOC = "IOC"
    FOK = "FOK"


class OrderPurpose(StrEnum):
    """Why an order exists.

    The distinction is not cosmetic: only an ``ENTRY`` requires a risk approval,
    and a halted day must still be able to submit the exit orders that protect
    an open position.
    """

    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL_EXIT = "MANUAL_EXIT"
    SESSION_CLOSE = "SESSION_CLOSE"
    RECONCILIATION_CLOSE = "RECONCILIATION_CLOSE"

    @property
    def opens_exposure(self) -> bool:
        """Whether this order increases exposure and therefore needs approval."""
        return self is OrderPurpose.ENTRY


class ExitReason(StrEnum):
    """Why a position was closed. Every trade must state one."""

    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    MANUAL = "MANUAL"
    SESSION_CLOSE = "SESSION_CLOSE"
    DAY_STOP = "DAY_STOP"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    RECONCILIATION = "RECONCILIATION"


class ExecutionVenue(StrEnum):
    """Where an order was actually executed.

    Stored on every order so a paper fill can never be mistaken for a real one,
    including in a report produced months later.
    """

    PAPER = "PAPER"
    LIVE = "LIVE"
    BACKTEST = "BACKTEST"


class Timeframe(StrEnum):
    M15 = "15m"
    H1 = "1h"
    H4 = "4h"
    D1 = "1d"

    @property
    def minutes(self) -> int:
        return {
            Timeframe.M15: 15,
            Timeframe.H1: 60,
            Timeframe.H4: 240,
            Timeframe.D1: 1440,
        }[self]

    @property
    def duration(self) -> timedelta:
        return timedelta(minutes=self.minutes)


class CandleSource(StrEnum):
    """Where a stored candle came from.

    Recorded because the provenance matters when data is questioned: a candle
    backfilled after a gap was fetched under different conditions from one that
    arrived live, and a report should be able to tell them apart.
    """

    REST_BACKFILL = "REST_BACKFILL"
    REST_GAP_FILL = "REST_GAP_FILL"
    WEBSOCKET = "WEBSOCKET"


class RiskVerdict(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RuleAction(StrEnum):
    """What triggering a rule causes. See docs/RISK_RULES.md section 2.

    Ordered by how far the consequence reaches, which is what lets the engine
    aggregate several triggered rules into one outcome without a chain of
    conditionals deciding which matters most.
    """

    #: Makes a new session possible. Never causes one, and never refuses.
    ALLOW_RE_EVALUATION = "ALLOW_RE_EVALUATION"
    #: This proposal is refused. The day and the session continue.
    REJECT_ORDER = "REJECT_ORDER"
    #: The session closes. A new one may start only if the restart rule allows.
    CLOSE_SESSION = "CLOSE_SESSION"
    #: The day moves to DAILY_STOP_REACHED. No further entries today - but open
    #: positions are still managed to exit.
    HALT_DAY = "HALT_DAY"
    #: Everything is refused until an operator clears the condition.
    REJECT_ALL = "REJECT_ALL"

    @property
    def refuses_the_proposal(self) -> bool:
        """Whether triggering this rule prevents the trade under evaluation.

        ``ALLOW_RE_EVALUATION`` is the odd one out: R-08 triggering is a
        permission, not a refusal, and treating "triggered" as "bad" for every
        rule would silently invert it.
        """
        return self is not RuleAction.ALLOW_RE_EVALUATION

    @property
    def reach(self) -> int:
        """How far the consequence extends. Higher wins when several trigger."""
        return {
            RuleAction.ALLOW_RE_EVALUATION: 0,
            RuleAction.REJECT_ORDER: 1,
            RuleAction.CLOSE_SESSION: 2,
            RuleAction.HALT_DAY: 3,
            RuleAction.REJECT_ALL: 4,
        }[self]


class RuleStatus(StrEnum):
    """The outcome of evaluating one rule.

    Four states rather than a boolean, because "did not refuse" hides three
    genuinely different situations, and an operator reading an audit record
    needs to tell them apart.
    """

    #: Evaluated against real inputs; the condition was not met.
    PASSED = "PASSED"
    #: Evaluated; the condition was met, so the rule's action applies.
    TRIGGERED = "TRIGGERED"
    #: Deliberately switched off, for example R-23 by decision OD-03. Not a
    #: gap - a decision, and one that is visible in the record.
    NOT_APPLICABLE = "NOT_APPLICABLE"
    #: The threshold has never been calibrated. Refuses, because an
    #: uncalibrated mandatory gate is a gate nobody has measured, and treating
    #: an unknown limit as "no limit" is how a protection becomes decorative.
    NOT_CALIBRATED = "NOT_CALIBRATED"

    @property
    def blocks(self) -> bool:
        return self in {RuleStatus.TRIGGERED, RuleStatus.NOT_CALIBRATED}


class DailyPnlBasis(StrEnum):
    """Which P&L the daily loss limit is evaluated against (rule R-26).

    Phase 0 decision OD-06 selected the conservative basis: an open position
    sitting at -41 RON stops the day immediately, without waiting for it to
    close.
    """

    REALISED_ONLY = "REALISED_ONLY"
    REALISED_PLUS_UNREALISED = "REALISED_PLUS_UNREALISED"


class EventSeverity(StrEnum):
    """Severity of a technical system event."""

    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class AuditActor(StrEnum):
    """Who caused an audited action."""

    SYSTEM = "SYSTEM"
    OPERATOR = "OPERATOR"
    STRATEGY = "STRATEGY"
    RISK_ENGINE = "RISK_ENGINE"
    EXCHANGE = "EXCHANGE"
    SCHEDULER = "SCHEDULER"


class TradingOutcome(StrEnum):
    """Reportable outcome of a trading day or evaluation cycle.

    ``NO_TRADE`` is a valid, expected result. It is not an error.
    """

    ACTIVE_TRADING = "ACTIVE_TRADING"
    NO_TRADE = "NO_TRADE"
    TRADING_SUSPENDED = "TRADING_SUSPENDED"
    DAILY_TARGET_REACHED = "DAILY_TARGET_REACHED"
    DAILY_STOP_REACHED = "DAILY_STOP_REACHED"
    MANUALLY_STOPPED = "MANUALLY_STOPPED"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"


class RiskReasonCode(StrEnum):
    """Machine-readable reasons a risk rule approved or refused an action.

    This list is the single source of truth and is kept identical to the block
    in docs/RISK_RULES.md by an automated test.
    """

    RISK_PER_TRADE_EXCEEDED = "RISK_PER_TRADE_EXCEEDED"
    DAILY_LOSS_LIMIT_REACHED = "DAILY_LOSS_LIMIT_REACHED"
    MAX_OPEN_POSITIONS_REACHED = "MAX_OPEN_POSITIONS_REACHED"
    MAX_TRADES_PER_DAY_REACHED = "MAX_TRADES_PER_DAY_REACHED"
    MAX_CONSECUTIVE_LOSSES_REACHED = "MAX_CONSECUTIVE_LOSSES_REACHED"
    SESSION_TARGET_REACHED = "SESSION_TARGET_REACHED"
    SESSION_RESTART_ELIGIBLE = "SESSION_RESTART_ELIGIBLE"
    STALE_MARKET_DATA = "STALE_MARKET_DATA"
    SIGNAL_EXPIRED = "SIGNAL_EXPIRED"
    SPREAD_TOO_WIDE = "SPREAD_TOO_WIDE"
    LIQUIDITY_TOO_LOW = "LIQUIDITY_TOO_LOW"
    VOLATILITY_OUT_OF_RANGE = "VOLATILITY_OUT_OF_RANGE"
    REWARD_RISK_TOO_LOW = "REWARD_RISK_TOO_LOW"
    ESTIMATED_SLIPPAGE_TOO_HIGH = "ESTIMATED_SLIPPAGE_TOO_HIGH"
    EXCHANGE_FILTER_VIOLATION = "EXCHANGE_FILTER_VIOLATION"
    MIN_NOTIONAL_NOT_MET = "MIN_NOTIONAL_NOT_MET"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    SYSTEM_HEALTH_DEGRADED = "SYSTEM_HEALTH_DEGRADED"
    EXCHANGE_UNHEALTHY = "EXCHANGE_UNHEALTHY"
    EMERGENCY_STOP_ACTIVE = "EMERGENCY_STOP_ACTIVE"
    TRADING_WINDOW_CLOSED = "TRADING_WINDOW_CLOSED"
    CLOCK_DRIFT_EXCEEDED = "CLOCK_DRIFT_EXCEEDED"
    DAILY_PROFIT_FLOOR_REACHED = "DAILY_PROFIT_FLOOR_REACHED"
    DAY_BOUNDARY_NO_ENTRY_WINDOW = "DAY_BOUNDARY_NO_ENTRY_WINDOW"
    NO_VALID_OPPORTUNITY = "NO_VALID_OPPORTUNITY"
    STRATEGY_DISABLED = "STRATEGY_DISABLED"
    #: A mandatory gate has never been calibrated, so the engine cannot judge
    #: it. Distinct from the gate's own code on purpose: reporting
    #: SPREAD_TOO_WIDE when the maximum spread was never set would tell an
    #: operator the market was bad, when in fact the configuration is missing.
    RISK_CONFIGURATION_INCOMPLETE = "RISK_CONFIGURATION_INCOMPLETE"
