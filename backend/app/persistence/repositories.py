"""Repositories.

Thin, explicit data-access objects rather than a generic base class. A generic
repository saves a few lines and hides the operations that actually matter -
here, activating a configuration version, which must deactivate the previous one
in the same transaction or violate a database constraint.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import (
    AuditActor,
    EventSeverity,
    HealthStatus,
    TradingDayStatus,
    TradingSessionStatus,
)
from app.persistence.mixins import utc_now
from app.persistence.models import (
    AuditEvent,
    Exchange,
    FxRateSnapshot,
    RiskConfiguration,
    SystemEvent,
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
