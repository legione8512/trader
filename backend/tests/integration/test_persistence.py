"""Persistence tests against a real PostgreSQL."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import AuditActor, AutonomyMode, DailyPnlBasis, EventSeverity, Timeframe
from app.persistence.models import (
    AuditEvent,
    Exchange,
    FxRateSnapshot,
    RiskConfiguration,
    TradingConfiguration,
    TradingPair,
)
from app.persistence.repositories import (
    AuditRepository,
    ConfigurationRepository,
    ExchangeRepository,
    FxRateRepository,
    SystemEventRepository,
)

pytestmark = pytest.mark.integration


def make_risk_configuration(version: int, **overrides: Any) -> RiskConfiguration:
    values: dict[str, Any] = {
        "version": version,
        "reference_capital_ron": Decimal("1000.00"),
        "session_target_percent": Decimal("2.00"),
        "session_restart_threshold_percent": Decimal("4.00"),
        "daily_maximum_loss_percent": Decimal("4.00"),
        "daily_pnl_basis": DailyPnlBasis.REALISED_PLUS_UNREALISED,
        "maximum_risk_per_trade_percent": Decimal("0.50"),
        "maximum_open_positions": 1,
        "maximum_trades_per_day": 50,
        "maximum_consecutive_losses": 3,
        "no_new_entry_minutes_before_day_end": 30,
    }
    values.update(overrides)
    return RiskConfiguration(**values)


class TestNumericExactness:
    """AC-17 in the database, not only in Python."""

    async def test_a_decimal_survives_a_round_trip_unchanged(
        self, db_session: AsyncSession
    ) -> None:
        configuration = make_risk_configuration(
            1,
            reference_capital_ron=Decimal("1000.00"),
            maximum_risk_per_trade_percent=Decimal("0.50"),
        )
        db_session.add(configuration)
        await db_session.flush()
        db_session.expunge_all()

        loaded = await db_session.get(RiskConfiguration, configuration.id)
        assert loaded is not None
        assert isinstance(loaded.reference_capital_ron, Decimal)
        assert loaded.reference_capital_ron == Decimal("1000.00")
        assert loaded.maximum_risk_per_trade_percent == Decimal("0.50")

    async def test_a_value_binary_float_cannot_represent(self, db_session: AsyncSession) -> None:
        """0.1 + 0.2 == 0.3 exactly, which is false for float."""
        first = make_risk_configuration(1, reference_capital_ron=Decimal("0.10"))
        second = make_risk_configuration(2, reference_capital_ron=Decimal("0.20"))
        db_session.add_all([first, second])
        await db_session.flush()

        result = await db_session.execute(
            select(RiskConfiguration.reference_capital_ron).order_by(RiskConfiguration.version)
        )
        values = list(result.scalars().all())
        assert sum(values, Decimal(0)) == Decimal("0.30")

    async def test_the_column_type_is_numeric_not_float(self, db_session: AsyncSession) -> None:
        result = await db_session.execute(
            text(
                "SELECT data_type, numeric_precision, numeric_scale "
                "FROM information_schema.columns "
                "WHERE table_name = 'risk_configuration' "
                "AND column_name = 'reference_capital_ron'"
            )
        )
        data_type, precision, scale = result.one()
        assert data_type == "numeric"
        assert (precision, scale) == (24, 8)


class TestUtcTimestamps:
    async def test_a_naive_datetime_is_rejected(self, db_session: AsyncSession) -> None:
        """Silently assuming naive means UTC is how a day boundary shifts."""
        event = AuditEvent(
            occurred_at=datetime(2026, 7, 28, 12, 0, 0),  # noqa: DTZ001 - deliberate
            event_type="TEST",
            payload={},
        )
        db_session.add(event)
        with pytest.raises((StatementError, ValueError)):
            await db_session.flush()

    async def test_an_aware_timestamp_comes_back_as_utc(self, db_session: AsyncSession) -> None:
        bucharest_noon = datetime(2026, 7, 28, 12, 0, tzinfo=UTC) + timedelta(hours=3)
        event = AuditEvent(occurred_at=bucharest_noon, event_type="TEST", payload={})
        db_session.add(event)
        await db_session.flush()
        db_session.expunge_all()

        loaded = await db_session.get(AuditEvent, event.id)
        assert loaded is not None
        assert loaded.occurred_at.tzinfo is not None
        assert loaded.occurred_at == bucharest_noon


class TestOnlyOneActiveConfiguration:
    async def test_two_active_risk_configurations_are_impossible(
        self, db_session: AsyncSession
    ) -> None:
        """A database guarantee, not an application habit."""
        db_session.add(make_risk_configuration(1, is_active=True))
        await db_session.flush()

        db_session.add(make_risk_configuration(2, is_active=True))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_many_inactive_versions_are_allowed(self, db_session: AsyncSession) -> None:
        """History is never deleted; only one version is current."""
        db_session.add_all(
            [make_risk_configuration(version, is_active=False) for version in range(1, 6)]
        )
        await db_session.flush()

        result = await db_session.execute(select(RiskConfiguration))
        assert len(result.scalars().all()) == 5

    async def test_activating_a_new_version_deactivates_the_previous_one(
        self, db_session: AsyncSession
    ) -> None:
        repository = ConfigurationRepository(db_session)

        first = make_risk_configuration(1)
        await repository.activate_risk_configuration(first)
        assert (await repository.get_active_risk_configuration()) is not None

        second = make_risk_configuration(2, maximum_trades_per_day=25)
        await repository.activate_risk_configuration(second)

        active = await repository.get_active_risk_configuration()
        assert active is not None
        assert active.version == 2
        assert active.maximum_trades_per_day == 25

        # The old version is still readable, which is what makes a stored risk
        # assessment explainable months later.
        historical = await repository.get_risk_configuration_by_version(1)
        assert historical is not None
        assert historical.is_active is False
        assert historical.maximum_trades_per_day == 50


class TestCheckConstraints:
    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("reference_capital_ron", Decimal("0")),
            ("maximum_open_positions", 0),
            ("maximum_trades_per_day", 0),
            ("maximum_consecutive_losses", 0),
            ("daily_maximum_loss_percent", Decimal("0")),
            ("no_new_entry_minutes_before_day_end", 2000),
        ],
    )
    async def test_invalid_risk_values_are_refused_by_the_database(
        self, db_session: AsyncSession, field: str, value: object
    ) -> None:
        db_session.add(make_risk_configuration(1, **{field: value}))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_an_unknown_enum_value_is_refused(self, db_session: AsyncSession) -> None:
        """The enum is VARCHAR plus a CHECK constraint, validated server-side."""
        with pytest.raises((IntegrityError, DBAPIError)):
            await db_session.execute(
                text(
                    "INSERT INTO trading_configuration "
                    "(id, version, is_active, autonomy_mode, emergency_stop_active, "
                    " reporting_currency, exchange_quote_currency, trading_timezone, "
                    " primary_timeframe, fx_rate_source, usdt_usd_peg, created_by, created_at) "
                    "VALUES (gen_random_uuid(), 99, false, 'GOD_MODE', false, "
                    " 'RON', 'USDT', 'Europe/Bucharest', '15m', 'BNR', 1.0, 'TEST', now())"
                )
            )
            await db_session.flush()


class TestFxRateSnapshots:
    async def test_a_weekend_reuses_the_last_published_rate(self, db_session: AsyncSession) -> None:
        """BNR publishes on working days only. The gap must stay visible."""
        repository = FxRateRepository(db_session)
        friday = date(2026, 7, 24)
        await repository.add(
            FxRateSnapshot(
                source="BNR",
                base_currency="USD",
                quote_currency="RON",
                rate=Decimal("4.567890123456"),
                rate_date=friday,
                fetched_at=datetime.now(UTC),
            )
        )

        sunday = date(2026, 7, 26)
        assert await repository.get_for_date("BNR", "USD", "RON", sunday) is None

        carried = await repository.get_latest_on_or_before("BNR", "USD", "RON", sunday)
        assert carried is not None
        # The rate_date stays Friday, which is what makes the reuse visible
        # instead of silently presenting it as Sunday's rate.
        assert carried.rate_date == friday
        assert carried.rate == Decimal("4.567890123456")

    async def test_the_same_source_pair_and_date_cannot_be_stored_twice(
        self, db_session: AsyncSession
    ) -> None:
        snapshot_values = {
            "source": "BNR",
            "base_currency": "USD",
            "quote_currency": "RON",
            "rate_date": date(2026, 7, 24),
            "fetched_at": datetime.now(UTC),
        }
        db_session.add(FxRateSnapshot(rate=Decimal("4.5"), **snapshot_values))
        await db_session.flush()

        db_session.add(FxRateSnapshot(rate=Decimal("4.6"), **snapshot_values))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_a_rate_between_identical_currencies_is_refused(
        self, db_session: AsyncSession
    ) -> None:
        db_session.add(
            FxRateSnapshot(
                source="BNR",
                base_currency="RON",
                quote_currency="RON",
                rate=Decimal("1"),
                rate_date=date(2026, 7, 24),
                fetched_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestAuditTrail:
    async def test_events_for_one_aggregate_come_back_in_order(
        self, db_session: AsyncSession
    ) -> None:
        repository = AuditRepository(db_session)
        order_id = uuid.uuid4()

        for index, event_type in enumerate(
            ["ORDER_INTENT_RECORDED", "ORDER_SUBMITTED", "ORDER_FILLED"]
        ):
            await repository.record(
                event_type=event_type,
                aggregate_type="Order",
                aggregate_id=order_id,
                payload={"step": index},
            )

        events = await repository.list_for_aggregate("Order", order_id)
        assert [event.event_type for event in events] == [
            "ORDER_INTENT_RECORDED",
            "ORDER_SUBMITTED",
            "ORDER_FILLED",
        ]

    async def test_a_correlation_id_links_a_whole_decision_chain(
        self, db_session: AsyncSession
    ) -> None:
        repository = AuditRepository(db_session)
        correlation_id = uuid.uuid4()

        await repository.record(
            event_type="SIGNAL_GENERATED",
            aggregate_type="Signal",
            aggregate_id=uuid.uuid4(),
            correlation_id=correlation_id,
            actor=AuditActor.STRATEGY,
        )
        await repository.record(
            event_type="RISK_ASSESSED",
            aggregate_type="RiskAssessment",
            aggregate_id=uuid.uuid4(),
            correlation_id=correlation_id,
            actor=AuditActor.RISK_ENGINE,
        )

        chain = await repository.list_for_correlation(correlation_id)
        assert len(chain) == 2
        assert {event.actor for event in chain} == {AuditActor.STRATEGY, AuditActor.RISK_ENGINE}

    async def test_the_payload_survives_as_structured_json(self, db_session: AsyncSession) -> None:
        repository = AuditRepository(db_session)
        event = await repository.record(
            event_type="RISK_REJECTED",
            payload={
                "reasonCodes": ["STALE_MARKET_DATA", "SPREAD_TOO_WIDE"],
                "inputs": {"spreadBps": "12.5"},
            },
            summary="Refused: market data was stale and the spread was too wide.",
        )
        db_session.expunge_all()

        loaded = await db_session.get(AuditEvent, event.id)
        assert loaded is not None
        assert loaded.payload["reasonCodes"] == ["STALE_MARKET_DATA", "SPREAD_TOO_WIDE"]
        assert loaded.payload["inputs"]["spreadBps"] == "12.5"
        assert loaded.summary is not None

    async def test_the_audit_table_has_no_updated_at_column(self, db_session: AsyncSession) -> None:
        """A record that can be modified is not an audit trail."""
        result = await db_session.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'audit_event'"
            )
        )
        columns = {row[0] for row in result.all()}
        assert "created_at" in columns
        assert "updated_at" not in columns


class TestSystemEvents:
    async def test_recording_a_degraded_health_event(self, db_session: AsyncSession) -> None:
        repository = SystemEventRepository(db_session)
        event = await repository.record(
            severity=EventSeverity.WARNING,
            category="MARKET_DATA",
            message="Candle stream reconnected after 3 seconds.",
            detail={"gapSeconds": 3},
        )
        assert event.severity is EventSeverity.WARNING


class TestReferenceData:
    async def test_the_same_symbol_cannot_be_registered_twice_on_one_exchange(
        self, db_session: AsyncSession
    ) -> None:
        repository = ExchangeRepository(db_session)
        exchange = await repository.add(Exchange(code="BINANCE", name="Binance Spot"))

        await repository.add_pair(
            TradingPair(
                exchange_id=exchange.id, symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"
            )
        )
        db_session.add(
            TradingPair(
                exchange_id=exchange.id, symbol="BTCUSDT", base_asset="BTC", quote_asset="USDT"
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_exchange_filters_are_stored_verbatim(self, db_session: AsyncSession) -> None:
        """Stored as the exchange returns them, not reshaped into our guesses."""
        repository = ExchangeRepository(db_session)
        exchange = await repository.add(Exchange(code="BINANCE", name="Binance Spot"))
        pair = await repository.add_pair(
            TradingPair(
                exchange_id=exchange.id,
                symbol="BTCUSDT",
                base_asset="BTC",
                quote_asset="USDT",
                filters={"filters": [{"filterType": "LOT_SIZE", "stepSize": "0.00001000"}]},
            )
        )
        db_session.expunge_all()

        loaded = await db_session.get(TradingPair, pair.id)
        assert loaded is not None
        assert loaded.filters is not None
        assert loaded.filters["filters"][0]["stepSize"] == "0.00001000"


class TestConfigurationDefaults:
    async def test_a_trading_configuration_defaults_to_signal_only(
        self, db_session: AsyncSession
    ) -> None:
        configuration = TradingConfiguration(version=1, usdt_usd_peg=Decimal("1.0000"))
        db_session.add(configuration)
        await db_session.flush()
        db_session.expunge_all()

        loaded = await db_session.get(TradingConfiguration, configuration.id)
        assert loaded is not None
        assert loaded.autonomy_mode is AutonomyMode.SIGNAL_ONLY
        assert loaded.emergency_stop_active is False
        assert loaded.primary_timeframe is Timeframe.M15
        assert loaded.trading_timezone == "Europe/Bucharest"
