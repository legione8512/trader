"""Repositories.

Thin, explicit data-access objects rather than a generic base class. A generic
repository saves a few lines and hides the operations that actually matter -
here, activating a configuration version, which must deactivate the previous one
in the same transaction or violate a database constraint.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    AuditActor,
    EventSeverity,
    ExecutionVenue,
    HealthStatus,
    OrderStatus,
    PositionStatus,
    RiskVerdict,
    SignalStatus,
    TradingDayStatus,
    TradingSessionStatus,
)
from app.persistence.mixins import utc_now
from app.persistence.models import (
    AuditEvent,
    BalanceSnapshot,
    Exchange,
    FxRateSnapshot,
    Order,
    OrderFill,
    PnLSnapshot,
    Position,
    RiskAssessment,
    RiskConfiguration,
    Signal,
    Strategy,
    StrategyVersion,
    SystemEvent,
    Trade,
    TradingConfiguration,
    TradingDay,
    TradingPair,
    TradingSession,
)


class ConfigurationRepository:
    """Reads the active configuration and creates new versions.

    Configuration is never updated in place. Changing a risk parameter creates
    version N+1 and deactivates version N, so a risk assessment stored last week
    can still be explained with the exact numbers that produced it.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------- risk ----

    async def get_active_risk_configuration(self) -> RiskConfiguration | None:
        result = await self._session.execute(
            select(RiskConfiguration).where(RiskConfiguration.is_active.is_(True))
        )
        return result.scalar_one_or_none()

    async def get_risk_configuration_by_version(self, version: int) -> RiskConfiguration | None:
        result = await self._session.execute(
            select(RiskConfiguration).where(RiskConfiguration.version == version)
        )
        return result.scalar_one_or_none()

    async def next_risk_version(self) -> int:
        result = await self._session.execute(
            select(RiskConfiguration.version).order_by(RiskConfiguration.version.desc()).limit(1)
        )
        highest = result.scalar_one_or_none()
        return 1 if highest is None else highest + 1

    async def activate_risk_configuration(self, configuration: RiskConfiguration) -> None:
        """Make one version active, deactivating every other one.

        The deactivation must be flushed BEFORE the new row becomes active: a
        partial unique index allows only one active row, so the opposite order
        would fail on the constraint.
        """
        await self._session.execute(
            update(RiskConfiguration)
            .where(RiskConfiguration.is_active.is_(True))
            .values(is_active=False)
        )
        await self._session.flush()

        configuration.is_active = True
        self._session.add(configuration)
        await self._session.flush()

    # ---------------------------------------------------------- trading ----

    async def get_active_trading_configuration(self) -> TradingConfiguration | None:
        result = await self._session.execute(
            select(TradingConfiguration).where(TradingConfiguration.is_active.is_(True))
        )
        return result.scalar_one_or_none()

    async def next_trading_version(self) -> int:
        result = await self._session.execute(
            select(TradingConfiguration.version)
            .order_by(TradingConfiguration.version.desc())
            .limit(1)
        )
        highest = result.scalar_one_or_none()
        return 1 if highest is None else highest + 1

    async def activate_trading_configuration(self, configuration: TradingConfiguration) -> None:
        await self._session.execute(
            update(TradingConfiguration)
            .where(TradingConfiguration.is_active.is_(True))
            .values(is_active=False)
        )
        await self._session.flush()

        configuration.is_active = True
        self._session.add(configuration)
        await self._session.flush()


class ExchangeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_code(self, code: str) -> Exchange | None:
        result = await self._session.execute(select(Exchange).where(Exchange.code == code))
        return result.scalar_one_or_none()

    async def add(self, exchange: Exchange) -> Exchange:
        self._session.add(exchange)
        await self._session.flush()
        return exchange

    async def get_pair(self, exchange_id: uuid.UUID, symbol: str) -> TradingPair | None:
        result = await self._session.execute(
            select(TradingPair).where(
                TradingPair.exchange_id == exchange_id,
                TradingPair.symbol == symbol,
            )
        )
        return result.scalar_one_or_none()

    async def list_enabled_pairs(self, exchange_id: uuid.UUID) -> Sequence[TradingPair]:
        result = await self._session.execute(
            select(TradingPair)
            .where(TradingPair.exchange_id == exchange_id, TradingPair.is_enabled.is_(True))
            .order_by(TradingPair.symbol)
        )
        return result.scalars().all()

    async def add_pair(self, pair: TradingPair) -> TradingPair:
        self._session.add(pair)
        await self._session.flush()
        return pair


class FxRateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_for_date(
        self, source: str, base_currency: str, quote_currency: str, rate_date: date
    ) -> FxRateSnapshot | None:
        result = await self._session.execute(
            select(FxRateSnapshot).where(
                FxRateSnapshot.source == source,
                FxRateSnapshot.base_currency == base_currency,
                FxRateSnapshot.quote_currency == quote_currency,
                FxRateSnapshot.rate_date == rate_date,
            )
        )
        return result.scalar_one_or_none()

    async def get_latest_on_or_before(
        self, source: str, base_currency: str, quote_currency: str, rate_date: date
    ) -> FxRateSnapshot | None:
        """The rate to use for a day the source did not publish.

        BNR publishes on working days only. For a Sunday this returns Friday's
        rate, still carrying Friday's ``rate_date`` so the reuse stays visible.
        """
        result = await self._session.execute(
            select(FxRateSnapshot)
            .where(
                FxRateSnapshot.source == source,
                FxRateSnapshot.base_currency == base_currency,
                FxRateSnapshot.quote_currency == quote_currency,
                FxRateSnapshot.rate_date <= rate_date,
            )
            .order_by(FxRateSnapshot.rate_date.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def add(self, snapshot: FxRateSnapshot) -> FxRateSnapshot:
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot


class TradingDayRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_date(self, trading_date: date) -> TradingDay | None:
        result = await self._session.execute(
            select(TradingDay).where(TradingDay.trading_date == trading_date)
        )
        return result.scalar_one_or_none()

    async def add(self, day: TradingDay) -> TradingDay:
        self._session.add(day)
        await self._session.flush()
        return day

    async def list_unclosed(self) -> Sequence[TradingDay]:
        """Days that never reached CLOSED.

        On startup this is what tells the application it crashed mid-day and
        must reconcile before doing anything else.
        """
        result = await self._session.execute(
            select(TradingDay)
            .where(TradingDay.status != TradingDayStatus.CLOSED)
            .order_by(TradingDay.trading_date)
        )
        return result.scalars().all()


class TradingSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, trading_session: TradingSession) -> TradingSession:
        self._session.add(trading_session)
        await self._session.flush()
        return trading_session

    async def next_sequence(self, trading_day_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(TradingSession.sequence)
            .where(TradingSession.trading_day_id == trading_day_id)
            .order_by(TradingSession.sequence.desc())
            .limit(1)
        )
        highest = result.scalar_one_or_none()
        return 1 if highest is None else highest + 1

    async def get_open_session(self, trading_day_id: uuid.UUID) -> TradingSession | None:
        """The session currently in progress, if any.

        At most one session is ever in progress: a new one starts only after the
        previous has reached a closed state.
        """
        result = await self._session.execute(
            select(TradingSession).where(
                TradingSession.trading_day_id == trading_day_id,
                TradingSession.status.in_(
                    [
                        TradingSessionStatus.EVALUATING,
                        TradingSessionStatus.OPEN,
                        TradingSessionStatus.CLOSING,
                    ]
                ),
            )
        )
        return result.scalar_one_or_none()

    async def list_for_day(self, trading_day_id: uuid.UUID) -> Sequence[TradingSession]:
        result = await self._session.execute(
            select(TradingSession)
            .where(TradingSession.trading_day_id == trading_day_id)
            .order_by(TradingSession.sequence)
        )
        return result.scalars().all()


class StrategyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_name(self, name: str) -> Strategy | None:
        result = await self._session.execute(select(Strategy).where(Strategy.name == name))
        return result.scalar_one_or_none()

    async def add(self, strategy: Strategy) -> Strategy:
        self._session.add(strategy)
        await self._session.flush()
        return strategy

    async def get_active_version(self, strategy_id: uuid.UUID) -> StrategyVersion | None:
        result = await self._session.execute(
            select(StrategyVersion).where(
                StrategyVersion.strategy_id == strategy_id,
                StrategyVersion.is_active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    async def next_version_number(self, strategy_id: uuid.UUID) -> int:
        result = await self._session.execute(
            select(StrategyVersion.version)
            .where(StrategyVersion.strategy_id == strategy_id)
            .order_by(StrategyVersion.version.desc())
            .limit(1)
        )
        highest = result.scalar_one_or_none()
        return 1 if highest is None else highest + 1

    async def activate_version(self, version: StrategyVersion) -> None:
        """Make one version active, deactivating the rest of that strategy.

        Deactivation is flushed first: a partial unique index allows only one
        active version per strategy, so the opposite order fails the constraint.
        """
        await self._session.execute(
            update(StrategyVersion)
            .where(
                StrategyVersion.strategy_id == version.strategy_id,
                StrategyVersion.is_active.is_(True),
            )
            .values(is_active=False)
        )
        await self._session.flush()

        version.is_active = True
        self._session.add(version)
        await self._session.flush()


class SignalRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, signal: Signal) -> Signal:
        self._session.add(signal)
        await self._session.flush()
        return signal

    async def list_for_day(self, trading_day_id: uuid.UUID) -> Sequence[Signal]:
        result = await self._session.execute(
            select(Signal)
            .where(Signal.trading_day_id == trading_day_id)
            .order_by(Signal.generated_at)
        )
        return result.scalars().all()

    async def list_awaiting_operator(self) -> Sequence[Signal]:
        """SIGNAL_ONLY mode: proposals the operator has not answered yet."""
        result = await self._session.execute(
            select(Signal)
            .where(Signal.status == SignalStatus.AWAITING_OPERATOR)
            .order_by(Signal.generated_at)
        )
        return result.scalars().all()

    async def list_expirable(self, at: datetime) -> Sequence[Signal]:
        """Signals past their expiry that have not been marked expired yet."""
        result = await self._session.execute(
            select(Signal)
            .where(
                Signal.expires_at.is_not(None),
                Signal.expires_at <= at,
                Signal.status.in_([SignalStatus.RISK_APPROVED, SignalStatus.AWAITING_OPERATOR]),
            )
            .order_by(Signal.expires_at)
        )
        return result.scalars().all()


class RiskAssessmentRepository:
    """Append-only, like the audit trail."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, assessment: RiskAssessment) -> RiskAssessment:
        self._session.add(assessment)
        await self._session.flush()
        return assessment

    async def list_for_signal(self, signal_id: uuid.UUID) -> Sequence[RiskAssessment]:
        result = await self._session.execute(
            select(RiskAssessment)
            .where(RiskAssessment.signal_id == signal_id)
            .order_by(RiskAssessment.evaluated_at)
        )
        return result.scalars().all()

    async def list_rejections_with_reason(
        self, reason_code: str, since: datetime | None = None
    ) -> Sequence[RiskAssessment]:
        """Every refusal caused by one specific rule.

        This is the question an operator actually asks - "why did we not trade
        last week?" - and the GIN index over ``reason_codes`` is what makes it
        an indexed lookup instead of a table scan.
        """
        statement = select(RiskAssessment).where(
            RiskAssessment.verdict == RiskVerdict.REJECTED,
            RiskAssessment.reason_codes.contains([reason_code]),
        )
        if since is not None:
            statement = statement.where(RiskAssessment.evaluated_at >= since)
        result = await self._session.execute(statement.order_by(RiskAssessment.evaluated_at))
        return result.scalars().all()


class OrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, order: Order) -> Order:
        self._session.add(order)
        await self._session.flush()
        return order

    async def get_by_client_order_id(self, client_order_id: str) -> Order | None:
        """Look an order up by the id WE generated.

        This is the reconciliation entry point: after a timeout the exchange is
        asked about this id, never sent a second order.
        """
        result = await self._session.execute(
            select(Order).where(Order.client_order_id == client_order_id)
        )
        return result.scalar_one_or_none()

    async def list_needing_reconciliation(self) -> Sequence[Order]:
        """Orders whose real state is unknown.

        Until this list is empty, the safe default is to stop opening new
        positions: the application does not know its own exposure.
        """
        result = await self._session.execute(
            select(Order)
            .where(Order.status.in_([OrderStatus.UNKNOWN, OrderStatus.RECONCILING]))
            .order_by(Order.intent_recorded_at)
        )
        return result.scalars().all()

    async def list_open_on_exchange(self, venue: ExecutionVenue) -> Sequence[Order]:
        result = await self._session.execute(
            select(Order)
            .where(
                Order.venue == venue,
                Order.status.in_([OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED]),
            )
            .order_by(Order.intent_recorded_at)
        )
        return result.scalars().all()

    async def add_fill(self, fill: OrderFill) -> OrderFill:
        self._session.add(fill)
        await self._session.flush()
        return fill


class PositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, position: Position) -> Position:
        self._session.add(position)
        await self._session.flush()
        return position

    async def list_occupying_slots(self, venue: ExecutionVenue) -> Sequence[Position]:
        """Positions that count against ``maximumOpenPositions`` (R-04).

        OPENING, OPEN, CLOSING and DESYNCED all occupy a slot. A position being
        closed has not released it yet, and a desynced one occupies it by
        definition: its real size is unknown.
        """
        occupying = [status for status in PositionStatus if status.occupies_a_position_slot]
        result = await self._session.execute(
            select(Position)
            .where(Position.venue == venue, Position.status.in_(occupying))
            .order_by(Position.opened_at)
        )
        return result.scalars().all()

    async def count_occupying_slots(self, venue: ExecutionVenue) -> int:
        return len(await self.list_occupying_slots(venue))

    async def list_desynced(self) -> Sequence[Position]:
        result = await self._session.execute(
            select(Position).where(Position.status == PositionStatus.DESYNCED)
        )
        return result.scalars().all()


class TradeRepository:
    """Append-only ledger."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, trade: Trade) -> Trade:
        self._session.add(trade)
        await self._session.flush()
        return trade

    async def list_closed_on_day(self, trading_day_id: uuid.UUID) -> Sequence[Trade]:
        """Trades whose realised P&L belongs to this day (OD-04)."""
        result = await self._session.execute(
            select(Trade)
            .where(Trade.closed_trading_day_id == trading_day_id)
            .order_by(Trade.closed_at)
        )
        return result.scalars().all()

    async def list_opened_on_day(self, trading_day_id: uuid.UUID) -> Sequence[Trade]:
        """Trades that count against this day's trade limit (R-05, OD-04)."""
        result = await self._session.execute(
            select(Trade)
            .where(Trade.opened_trading_day_id == trading_day_id)
            .order_by(Trade.opened_at)
        )
        return result.scalars().all()

    async def count_consecutive_losses(self, venue: ExecutionVenue) -> int:
        """Losses since the last win, most recent first (R-06).

        Counted across the whole ledger rather than per day: a losing streak
        does not reset because the clock passed midnight.
        """
        result = await self._session.execute(
            select(Trade).where(Trade.venue == venue).order_by(Trade.closed_at.desc()).limit(50)
        )
        streak = 0
        for trade in result.scalars().all():
            if trade.is_win:
                break
            streak += 1
        return streak


class SnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add_balance(self, snapshot: BalanceSnapshot) -> BalanceSnapshot:
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def latest_balance(self, venue: ExecutionVenue, asset: str) -> BalanceSnapshot | None:
        result = await self._session.execute(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.venue == venue, BalanceSnapshot.asset == asset)
            .order_by(BalanceSnapshot.taken_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def add_pnl(self, snapshot: PnLSnapshot) -> PnLSnapshot:
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def latest_pnl(self, trading_day_id: uuid.UUID) -> PnLSnapshot | None:
        result = await self._session.execute(
            select(PnLSnapshot)
            .where(PnLSnapshot.trading_day_id == trading_day_id)
            .order_by(PnLSnapshot.taken_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()


class AuditRepository:
    """Append-only. There is deliberately no update and no delete."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        event_type: str,
        actor: AuditActor = AuditActor.SYSTEM,
        aggregate_type: str | None = None,
        aggregate_id: uuid.UUID | None = None,
        actor_detail: str | None = None,
        correlation_id: uuid.UUID | None = None,
        previous_state: str | None = None,
        new_state: str | None = None,
        payload: dict[str, Any] | None = None,
        summary: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            occurred_at=utc_now(),
            event_type=event_type,
            actor=actor,
            actor_detail=actor_detail,
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            correlation_id=correlation_id,
            previous_state=previous_state,
            new_state=new_state,
            payload=payload if payload is not None else {},
            summary=summary,
        )
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_for_aggregate(
        self, aggregate_type: str, aggregate_id: uuid.UUID
    ) -> Sequence[AuditEvent]:
        """Every event about one entity, oldest first.

        This is what makes "every trade must be reconstructable" achievable.
        """
        result = await self._session.execute(
            select(AuditEvent)
            .where(
                AuditEvent.aggregate_type == aggregate_type,
                AuditEvent.aggregate_id == aggregate_id,
            )
            .order_by(AuditEvent.occurred_at, AuditEvent.created_at)
        )
        return result.scalars().all()

    async def list_for_correlation(self, correlation_id: uuid.UUID) -> Sequence[AuditEvent]:
        """Every event produced by one decision chain, oldest first."""
        result = await self._session.execute(
            select(AuditEvent)
            .where(AuditEvent.correlation_id == correlation_id)
            .order_by(AuditEvent.occurred_at, AuditEvent.created_at)
        )
        return result.scalars().all()


class SystemEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(
        self,
        *,
        severity: EventSeverity,
        category: str,
        message: str,
        detail: dict[str, Any] | None = None,
        health_status: HealthStatus | None = None,
    ) -> SystemEvent:
        event = SystemEvent(
            occurred_at=utc_now(),
            severity=severity,
            category=category,
            message=message,
            detail=detail,
            health_status=health_status,
        )
        self._session.add(event)
        await self._session.flush()
        return event
