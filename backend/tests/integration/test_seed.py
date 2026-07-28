"""Seeding tests."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.domain.enums import AutonomyMode, DailyPnlBasis
from app.persistence.models import Exchange, RiskConfiguration, TradingPair
from app.persistence.repositories import AuditRepository, ConfigurationRepository
from app.persistence.seed import BINANCE_CODE, seed

pytestmark = pytest.mark.integration


class TestSeeding:
    async def test_seeding_creates_configuration_and_reference_data(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        result = await seed(db_session, settings)

        assert result.exchange_created is True
        assert result.pairs_created == ("BTCUSDT", "ETHUSDT")
        assert result.risk_configuration_version == 1
        assert result.trading_configuration_version == 1

    async def test_seeding_is_idempotent(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        await seed(db_session, settings)
        second = await seed(db_session, settings)

        assert second.changed_anything is False
        assert second.pairs_created == ()
        assert second.risk_configuration_version is None

        result = await db_session.execute(select(RiskConfiguration))
        assert len(result.scalars().all()) == 1

    async def test_the_phase_zero_values_land_in_the_database(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        await seed(db_session, settings)

        configuration = await ConfigurationRepository(db_session).get_active_risk_configuration()
        assert configuration is not None
        assert configuration.reference_capital_ron == Decimal("1000.00")
        assert configuration.session_target_percent == Decimal("2.00")
        assert configuration.session_restart_threshold_percent == Decimal("4.00")
        assert configuration.daily_maximum_loss_percent == Decimal("4.00")
        assert configuration.maximum_risk_per_trade_percent == Decimal("0.50")
        assert configuration.maximum_open_positions == 1
        assert configuration.maximum_trades_per_day == 50
        assert configuration.maximum_consecutive_losses == 3
        assert configuration.no_new_entry_minutes_before_day_end == 30

    async def test_the_daily_profit_floor_is_disabled(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Phase 0 decision OD-03: no profit protection between sessions."""
        await seed(db_session, settings)

        configuration = await ConfigurationRepository(db_session).get_active_risk_configuration()
        assert configuration is not None
        assert configuration.daily_profit_giveback_percent is None

    async def test_the_daily_limit_uses_the_conservative_basis(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Phase 0 decision OD-06."""
        await seed(db_session, settings)

        configuration = await ConfigurationRepository(db_session).get_active_risk_configuration()
        assert configuration is not None
        assert configuration.daily_pnl_basis is DailyPnlBasis.REALISED_PLUS_UNREALISED

    async def test_market_quality_gates_stay_uncalibrated(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """NULL means "not yet calibrated", never "no limit".

        Phase 0 refused to invent these thresholds. They are set from real data
        in Phases 4 to 6.
        """
        await seed(db_session, settings)

        configuration = await ConfigurationRepository(db_session).get_active_risk_configuration()
        assert configuration is not None
        assert configuration.max_candle_age_seconds is None
        assert configuration.max_spread_bps is None
        assert configuration.min_reward_risk_ratio is None
        assert configuration.max_clock_drift_ms is None


class TestSeedingSafetyDefaults:
    async def test_trading_pairs_are_created_disabled(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Enabling a pair is an operator decision, never a side effect."""
        await seed(db_session, settings)

        result = await db_session.execute(select(TradingPair))
        pairs = result.scalars().all()
        assert len(pairs) == 2
        assert all(pair.is_enabled is False for pair in pairs)

    async def test_seeding_never_produces_a_system_armed_to_trade(
        self, db_session: AsyncSession, make_settings: Callable[..., Settings]
    ) -> None:
        """Even when the environment asks for an automatic mode.

        A fresh database must never come up ready to submit orders, whatever a
        stray environment variable says.
        """
        settings = make_settings(autonomy_mode=AutonomyMode.PAPER_AUTOMATIC)
        await seed(db_session, settings)

        configuration = await ConfigurationRepository(db_session).get_active_trading_configuration()
        assert configuration is not None
        assert configuration.autonomy_mode is AutonomyMode.SIGNAL_ONLY
        assert configuration.emergency_stop_active is False

    async def test_the_exchange_is_registered_with_its_stable_code(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        await seed(db_session, settings)

        result = await db_session.execute(select(Exchange).where(Exchange.code == BINANCE_CODE))
        exchange = result.scalar_one()
        assert exchange.name == "Binance Spot"


class TestSeedingIsAudited:
    async def test_every_seeded_entity_leaves_an_audit_record(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        await seed(db_session, settings)

        audit = AuditRepository(db_session)
        configuration = await ConfigurationRepository(db_session).get_active_risk_configuration()
        assert configuration is not None

        events = await audit.list_for_aggregate("RiskConfiguration", configuration.id)
        assert [event.event_type for event in events] == ["RISK_CONFIGURATION_ACTIVATED"]
        assert events[0].new_state == "ACTIVE"
        assert events[0].payload["referenceCapitalRon"] == "1000.00"
        assert events[0].payload["maximumTradesPerDay"] == 50
