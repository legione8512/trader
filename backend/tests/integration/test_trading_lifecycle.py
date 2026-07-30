"""Trading day and session lifecycle, against a real database."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.domain.enums import (
    AuditActor,
    RiskReasonCode,
    TradingDayStatus,
    TradingOutcome,
    TradingSessionStatus,
)
from app.domain.errors import InvalidTransitionError
from app.domain.state_machines import TRADING_DAY_MACHINE, TRADING_SESSION_MACHINE
from app.persistence.models import TradingDay, TradingSession
from app.persistence.repositories import (
    AuditRepository,
    ConfigurationRepository,
    TradingDayRepository,
    TradingSessionRepository,
)
from app.persistence.seed import seed
from app.persistence.state_transitions import apply_transition

pytestmark = pytest.mark.integration

FUNDING_RATE = Decimal("4.60")  # RON per USDT, locked at funding time


async def make_day(
    db_session: AsyncSession, settings: Settings, trading_date: date = date(2026, 7, 28)
) -> TradingDay:
    await seed(db_session, settings)
    configurations = ConfigurationRepository(db_session)
    risk = await configurations.get_active_risk_configuration()
    trading = await configurations.get_active_trading_configuration()
    assert risk is not None and trading is not None

    return await TradingDayRepository(db_session).add(
        TradingDay(
            trading_date=trading_date,
            timezone=trading.trading_timezone,
            risk_configuration_id=risk.id,
            trading_configuration_id=trading.id,
            reporting_currency=risk.reporting_currency,
            quote_currency=trading.exchange_quote_currency,
            reference_capital_ron=risk.reference_capital_ron,
            funding_rate_ron_per_quote=FUNDING_RATE,
        )
    )


class TestTradingDayCreation:
    async def test_a_new_day_starts_pending_with_zero_everything(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        day = await make_day(db_session, settings)
        db_session.expunge_all()

        loaded = await db_session.get(TradingDay, day.id)
        assert loaded is not None
        assert loaded.status is TradingDayStatus.PENDING
        assert loaded.realised_pnl_quote == Decimal(0)
        assert loaded.unrealised_pnl_quote == Decimal(0)
        assert loaded.trade_count == 0
        assert loaded.session_count == 0
        assert loaded.consecutive_losses == 0
        assert loaded.outcome is None

    async def test_the_day_snapshots_the_configuration_that_governs_it(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Without this, a decision cannot be explained after a config change."""
        day = await make_day(db_session, settings)
        assert day.reference_capital_ron == Decimal("1000.00")
        assert day.reporting_currency == "RON"
        assert day.quote_currency == "USDT"
        assert day.funding_rate_ron_per_quote == FUNDING_RATE
        assert day.risk_configuration_id is not None

    async def test_the_same_date_cannot_exist_twice(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """What makes "create today's day if absent" safe under concurrency."""
        day = await make_day(db_session, settings)
        db_session.add(
            TradingDay(
                trading_date=day.trading_date,
                timezone=day.timezone,
                risk_configuration_id=day.risk_configuration_id,
                trading_configuration_id=day.trading_configuration_id,
                reporting_currency="RON",
                quote_currency="USDT",
                reference_capital_ron=Decimal("1000.00"),
                funding_rate_ron_per_quote=FUNDING_RATE,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_the_net_pnl_basis_is_realised_plus_unrealised(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Rule R-26, Phase 0 decision OD-06."""
        day = await make_day(db_session, settings)
        day.realised_pnl_quote = Decimal("5.00")
        day.unrealised_pnl_quote = Decimal("-11.00")
        assert day.net_pnl_quote == Decimal("-6.00")


class TestAuditedTransitions:
    async def test_a_legal_transition_changes_state_and_leaves_a_record(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        day = await make_day(db_session, settings)
        audit = AuditRepository(db_session)

        await apply_transition(
            entity=day,
            machine=TRADING_DAY_MACHINE,
            target=TradingDayStatus.ACTIVE,
            audit=audit,
            aggregate_type="TradingDay",
            event_type="TRADING_DAY_ACTIVATED",
            summary="Preflight checks passed.",
        )

        assert day.status is TradingDayStatus.ACTIVE

        events = await audit.list_for_aggregate("TradingDay", day.id)
        assert len(events) == 1
        assert events[0].previous_state == "PENDING"
        assert events[0].new_state == "ACTIVE"
        assert events[0].event_type == "TRADING_DAY_ACTIVATED"

    async def test_an_illegal_transition_changes_nothing_at_all(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A refused transition must leave no trace: it did not happen."""
        day = await make_day(db_session, settings)
        audit = AuditRepository(db_session)

        with pytest.raises(InvalidTransitionError):
            await apply_transition(
                entity=day,
                machine=TRADING_DAY_MACHINE,
                target=TradingDayStatus.CLOSED,
                audit=audit,
                aggregate_type="TradingDay",
                event_type="TRADING_DAY_CLOSED",
            )

        assert day.status is TradingDayStatus.PENDING
        assert await audit.list_for_aggregate("TradingDay", day.id) == []

    async def test_the_reason_code_reaches_the_audit_payload(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        day = await make_day(db_session, settings)
        audit = AuditRepository(db_session)

        await apply_transition(
            entity=day,
            machine=TRADING_DAY_MACHINE,
            target=TradingDayStatus.ACTIVE,
            audit=audit,
            aggregate_type="TradingDay",
            event_type="TRADING_DAY_ACTIVATED",
        )
        await apply_transition(
            entity=day,
            machine=TRADING_DAY_MACHINE,
            target=TradingDayStatus.DAILY_STOP_REACHED,
            audit=audit,
            aggregate_type="TradingDay",
            event_type="TRADING_DAY_STOPPED",
            actor=AuditActor.RISK_ENGINE,
            reason=RiskReasonCode.DAILY_LOSS_LIMIT_REACHED.value,
            payload={"netPnlQuote": "-8.70"},
            summary="Daily loss limit reached.",
        )

        events = await audit.list_for_aggregate("TradingDay", day.id)
        assert len(events) == 2
        stop = events[-1]
        assert stop.actor is AuditActor.RISK_ENGINE
        assert stop.payload["reason"] == "DAILY_LOSS_LIMIT_REACHED"
        assert stop.payload["netPnlQuote"] == "-8.70"


class TestDayCannotResumeAfterStopping:
    """AC-05: a session restart never resets the daily loss allowance."""

    async def test_a_stopped_day_can_only_close(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        day = await make_day(db_session, settings)
        audit = AuditRepository(db_session)

        for target, event in (
            (TradingDayStatus.ACTIVE, "TRADING_DAY_ACTIVATED"),
            (TradingDayStatus.DAILY_STOP_REACHED, "TRADING_DAY_STOPPED"),
        ):
            await apply_transition(
                entity=day,
                machine=TRADING_DAY_MACHINE,
                target=target,
                audit=audit,
                aggregate_type="TradingDay",
                event_type=event,
            )

        with pytest.raises(InvalidTransitionError):
            await apply_transition(
                entity=day,
                machine=TRADING_DAY_MACHINE,
                target=TradingDayStatus.ACTIVE,
                audit=audit,
                aggregate_type="TradingDay",
                event_type="TRADING_DAY_REACTIVATED",
            )

        assert day.status is TradingDayStatus.DAILY_STOP_REACHED


class TestNoTradeDay:
    """AC-01: zero trades is a valid outcome, not an error."""

    async def test_a_day_with_no_trades_closes_normally(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        day = await make_day(db_session, settings)
        audit = AuditRepository(db_session)

        await apply_transition(
            entity=day,
            machine=TRADING_DAY_MACHINE,
            target=TradingDayStatus.ACTIVE,
            audit=audit,
            aggregate_type="TradingDay",
            event_type="TRADING_DAY_ACTIVATED",
        )
        await apply_transition(
            entity=day,
            machine=TRADING_DAY_MACHINE,
            target=TradingDayStatus.CLOSED,
            audit=audit,
            aggregate_type="TradingDay",
            event_type="TRADING_DAY_CLOSED",
            summary="No opportunity met the mandatory criteria today.",
        )
        day.outcome = TradingOutcome.NO_TRADE
        day.closed_at = datetime.now(UTC)
        await db_session.flush()

        assert day.status is TradingDayStatus.CLOSED
        assert day.outcome is TradingOutcome.NO_TRADE
        assert day.trade_count == 0
        assert day.realised_pnl_quote == Decimal(0)
        assert day.stop_reason is None


class TestSessions:
    async def test_sessions_are_numbered_within_the_day(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        day = await make_day(db_session, settings)
        sessions = TradingSessionRepository(db_session)

        assert await sessions.next_sequence(day.id) == 1
        await sessions.add(
            TradingSession(trading_day_id=day.id, sequence=1, started_at=datetime.now(UTC))
        )
        assert await sessions.next_sequence(day.id) == 2

    async def test_the_same_sequence_cannot_be_used_twice_in_a_day(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        day = await make_day(db_session, settings)
        sessions = TradingSessionRepository(db_session)
        await sessions.add(
            TradingSession(trading_day_id=day.id, sequence=1, started_at=datetime.now(UTC))
        )

        db_session.add(
            TradingSession(trading_day_id=day.id, sequence=1, started_at=datetime.now(UTC))
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_only_one_session_is_ever_in_progress(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        day = await make_day(db_session, settings)
        sessions = TradingSessionRepository(db_session)
        audit = AuditRepository(db_session)

        first = await sessions.add(
            TradingSession(trading_day_id=day.id, sequence=1, started_at=datetime.now(UTC))
        )
        assert await sessions.get_open_session(day.id) is not None

        for target in (TradingSessionStatus.OPEN, TradingSessionStatus.CLOSING):
            await apply_transition(
                entity=first,
                machine=TRADING_SESSION_MACHINE,
                target=target,
                audit=audit,
                aggregate_type="TradingSession",
                event_type="TRADING_SESSION_TRANSITION",
            )
        await apply_transition(
            entity=first,
            machine=TRADING_SESSION_MACHINE,
            target=TradingSessionStatus.CLOSED_RESTART_ELIGIBLE,
            audit=audit,
            aggregate_type="TradingSession",
            event_type="TRADING_SESSION_CLOSED",
            reason=RiskReasonCode.SESSION_RESTART_ELIGIBLE.value,
        )
        await db_session.flush()

        assert await sessions.get_open_session(day.id) is None

    async def test_restart_eligibility_does_not_start_a_session_by_itself(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """CLOSED_RESTART_ELIGIBLE is terminal: it only makes a new session possible."""
        day = await make_day(db_session, settings)
        session_row = await TradingSessionRepository(db_session).add(
            TradingSession(
                trading_day_id=day.id,
                sequence=1,
                started_at=datetime.now(UTC),
                status=TradingSessionStatus.CLOSED_RESTART_ELIGIBLE,
            )
        )
        assert TRADING_SESSION_MACHINE.is_terminal(session_row.status)

    async def test_a_session_that_never_traded_can_abort(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        day = await make_day(db_session, settings)
        audit = AuditRepository(db_session)
        session_row = await TradingSessionRepository(db_session).add(
            TradingSession(trading_day_id=day.id, sequence=1, started_at=datetime.now(UTC))
        )

        await apply_transition(
            entity=session_row,
            machine=TRADING_SESSION_MACHINE,
            target=TradingSessionStatus.ABORTED,
            audit=audit,
            aggregate_type="TradingSession",
            event_type="TRADING_SESSION_ABORTED",
            reason=RiskReasonCode.SYSTEM_HEALTH_DEGRADED.value,
        )

        assert session_row.status is TradingSessionStatus.ABORTED
        assert session_row.trade_count == 0


class TestRecovery:
    async def test_unclosed_days_are_discoverable_after_a_crash(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """On startup this is what says "you crashed mid-day, reconcile first"."""
        day = await make_day(db_session, settings)
        audit = AuditRepository(db_session)
        await apply_transition(
            entity=day,
            machine=TRADING_DAY_MACHINE,
            target=TradingDayStatus.ACTIVE,
            audit=audit,
            aggregate_type="TradingDay",
            event_type="TRADING_DAY_ACTIVATED",
        )
        await db_session.flush()

        days = TradingDayRepository(db_session)
        assert [d.id for d in await days.list_unclosed()] == [day.id]

        await apply_transition(
            entity=day,
            machine=TRADING_DAY_MACHINE,
            target=TradingDayStatus.CLOSED,
            audit=audit,
            aggregate_type="TradingDay",
            event_type="TRADING_DAY_CLOSED",
        )
        await db_session.flush()
        assert await days.list_unclosed() == []
