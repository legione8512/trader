"""Strategy, signal and risk assessment tests."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.domain.enums import (
    OrderSide,
    RiskReasonCode,
    RiskVerdict,
    SignalStatus,
    Timeframe,
    TradingDayStatus,
)
from app.domain.errors import InvalidTransitionError
from app.domain.state_machines import SIGNAL_MACHINE
from app.persistence.models import (
    RiskAssessment,
    Signal,
    Strategy,
    StrategyVersion,
    TradingDay,
    TradingPair,
)
from app.persistence.repositories import (
    AuditRepository,
    ConfigurationRepository,
    RiskAssessmentRepository,
    SignalRepository,
    StrategyRepository,
    TradingDayRepository,
)
from app.persistence.seed import seed
from app.persistence.state_transitions import apply_transition

pytestmark = pytest.mark.integration

FUNDING_RATE = Decimal("4.60")
GENERATED_AT = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


class Fixture:
    """Everything a signal needs to exist."""

    def __init__(
        self,
        day: TradingDay,
        pair: TradingPair,
        version: StrategyVersion,
        risk_config_id: uuid.UUID,
    ) -> None:
        self.day = day
        self.pair = pair
        self.version = version
        self.risk_config_id = risk_config_id


async def build_fixture(db_session: AsyncSession, settings: Settings) -> Fixture:
    await seed(db_session, settings)

    configurations = ConfigurationRepository(db_session)
    risk = await configurations.get_active_risk_configuration()
    trading = await configurations.get_active_trading_configuration()
    assert risk is not None and trading is not None

    day = await TradingDayRepository(db_session).add(
        TradingDay(
            trading_date=date(2026, 7, 28),
            timezone=trading.trading_timezone,
            status=TradingDayStatus.ACTIVE,
            risk_configuration_id=risk.id,
            trading_configuration_id=trading.id,
            reporting_currency=risk.reporting_currency,
            quote_currency=trading.exchange_quote_currency,
            reference_capital_ron=risk.reference_capital_ron,
            funding_rate_ron_per_quote=FUNDING_RATE,
        )
    )

    pair = (
        await db_session.execute(select(TradingPair).where(TradingPair.symbol == "BTCUSDT"))
    ).scalar_one()

    strategies = StrategyRepository(db_session)
    strategy = await strategies.add(Strategy(name="baseline_v1", is_enabled=True))
    version = StrategyVersion(
        strategy_id=strategy.id,
        version=await strategies.next_version_number(strategy.id),
        parameters={"emaFast": 20, "emaSlow": 50, "atrPeriod": 14},
        code_fingerprint="a" * 64,
    )
    await strategies.activate_version(version)

    return Fixture(day, pair, version, risk.id)


def make_signal(fixture: Fixture, **overrides: object) -> Signal:
    values: dict[str, object] = {
        "trading_day_id": fixture.day.id,
        "trading_pair_id": fixture.pair.id,
        "strategy_version_id": fixture.version.id,
        "side": OrderSide.BUY,
        "timeframe": Timeframe.M15,
        "generated_at": GENERATED_AT,
        "reference_price": Decimal("65000.00"),
        "stop_loss_price": Decimal("64350.00"),
        "take_profit_price": Decimal("66300.00"),
        "correlation_id": uuid.uuid4(),
    }
    values.update(overrides)
    return Signal(**values)


class TestStrategyVersioning:
    async def test_only_one_version_of_a_strategy_can_be_active(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        db_session.add(
            StrategyVersion(
                strategy_id=fixture.version.strategy_id,
                version=99,
                parameters={},
                is_active=True,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_activating_a_new_version_retires_the_previous_one(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        strategies = StrategyRepository(db_session)
        strategy_id = fixture.version.strategy_id

        second = StrategyVersion(
            strategy_id=strategy_id,
            version=await strategies.next_version_number(strategy_id),
            parameters={"emaFast": 12, "emaSlow": 26, "atrPeriod": 14},
        )
        await strategies.activate_version(second)

        active = await strategies.get_active_version(strategy_id)
        assert active is not None
        assert active.version == 2
        assert active.parameters["emaFast"] == 12

        # Version 1 remains readable: signals still reference it.
        db_session.expunge_all()
        first = await db_session.get(StrategyVersion, fixture.version.id)
        assert first is not None
        assert first.is_active is False
        assert first.parameters["emaFast"] == 20

    async def test_a_strategy_is_disabled_by_default(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Enabling a strategy is an operator decision, never a side effect."""
        await seed(db_session, settings)
        strategy = await StrategyRepository(db_session).add(Strategy(name="experimental"))
        assert strategy.is_enabled is False


class TestSignalIntegrity:
    async def test_a_signal_records_the_exact_strategy_version(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Parameters change; a signal must still say which set produced it."""
        fixture = await build_fixture(db_session, settings)
        signal = await SignalRepository(db_session).add(
            make_signal(fixture, strategy_inputs={"ema20": "64980.10", "atr": "412.55"})
        )
        db_session.expunge_all()

        loaded = await db_session.get(Signal, signal.id)
        assert loaded is not None
        assert loaded.strategy_version_id == fixture.version.id
        assert loaded.strategy_inputs is not None
        assert loaded.strategy_inputs["ema20"] == "64980.10"

    async def test_an_inverted_stop_is_refused_by_the_database(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A long entry with the stop above the price is a sign error, not a strategy."""
        fixture = await build_fixture(db_session, settings)
        db_session.add(
            make_signal(
                fixture,
                reference_price=Decimal("65000.00"),
                stop_loss_price=Decimal("65500.00"),
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_a_target_below_the_entry_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        db_session.add(make_signal(fixture, take_profit_price=Decimal("64000.00")))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_the_stop_distance_is_the_sizing_denominator(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        signal = make_signal(fixture)
        assert signal.stop_distance == Decimal("650.00")

    async def test_prices_survive_as_exact_decimals(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        signal = await SignalRepository(db_session).add(
            make_signal(fixture, reference_price=Decimal("65000.123456789012"))
        )
        db_session.expunge_all()

        loaded = await db_session.get(Signal, signal.id)
        assert loaded is not None
        assert loaded.reference_price == Decimal("65000.123456789012")


class TestSignalExpiry:
    async def test_a_signal_without_an_expiry_never_expires(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        assert make_signal(fixture).has_expired(GENERATED_AT + timedelta(days=1)) is False

    async def test_expiry_is_inclusive_of_the_boundary(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        expires_at = GENERATED_AT + timedelta(minutes=5)
        signal = make_signal(fixture, expires_at=expires_at)

        assert signal.has_expired(expires_at - timedelta(seconds=1)) is False
        assert signal.has_expired(expires_at) is True

    async def test_expirable_signals_are_discoverable(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        signals = SignalRepository(db_session)

        stale = await signals.add(
            make_signal(
                fixture,
                expires_at=GENERATED_AT + timedelta(minutes=5),
                status=SignalStatus.AWAITING_OPERATOR,
            )
        )
        await signals.add(
            make_signal(
                fixture,
                expires_at=GENERATED_AT + timedelta(hours=2),
                status=SignalStatus.AWAITING_OPERATOR,
            )
        )

        found = await signals.list_expirable(GENERATED_AT + timedelta(minutes=10))
        assert [s.id for s in found] == [stale.id]


class TestSignalCannotBypassRisk:
    async def test_a_generated_signal_cannot_jump_to_execution(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """AC-19 at the signal level: strategies propose, they do not execute."""
        fixture = await build_fixture(db_session, settings)
        signal = await SignalRepository(db_session).add(make_signal(fixture))
        audit = AuditRepository(db_session)

        for forbidden in (SignalStatus.ACCEPTED, SignalStatus.EXECUTED):
            with pytest.raises(InvalidTransitionError):
                await apply_transition(
                    entity=signal,
                    machine=SIGNAL_MACHINE,
                    target=forbidden,
                    audit=audit,
                    aggregate_type="Signal",
                    event_type="SIGNAL_ACCEPTED",
                )

        assert signal.status is SignalStatus.GENERATED
        assert await audit.list_for_aggregate("Signal", signal.id) == []


class TestRiskAssessment:
    async def test_a_rejection_without_reason_codes_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A refusal nobody can explain is worse than no refusal at all."""
        fixture = await build_fixture(db_session, settings)
        signal = await SignalRepository(db_session).add(make_signal(fixture))

        db_session.add(
            RiskAssessment(
                signal_id=signal.id,
                trading_day_id=fixture.day.id,
                risk_configuration_id=fixture.risk_config_id,
                verdict=RiskVerdict.REJECTED,
                reason_codes=[],
                evaluated_at=GENERATED_AT,
                correlation_id=signal.correlation_id,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_an_approval_needs_no_reason_codes(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        signal = await SignalRepository(db_session).add(make_signal(fixture))

        assessment = await RiskAssessmentRepository(db_session).add(
            RiskAssessment(
                signal_id=signal.id,
                trading_day_id=fixture.day.id,
                risk_configuration_id=fixture.risk_config_id,
                verdict=RiskVerdict.APPROVED,
                reason_codes=[],
                approved_quantity=Decimal("0.00167000"),
                approved_risk_quote=Decimal("1.08550000"),
                evaluated_at=GENERATED_AT,
                correlation_id=signal.correlation_id,
            )
        )
        assert assessment.is_approved is True

    async def test_refusals_are_searchable_by_reason(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """The question an operator actually asks: why did we not trade?"""
        fixture = await build_fixture(db_session, settings)
        signals = SignalRepository(db_session)
        assessments = RiskAssessmentRepository(db_session)

        for index, codes in enumerate(
            [
                [RiskReasonCode.STALE_MARKET_DATA.value],
                [RiskReasonCode.SPREAD_TOO_WIDE.value, RiskReasonCode.STALE_MARKET_DATA.value],
                [RiskReasonCode.REWARD_RISK_TOO_LOW.value],
            ]
        ):
            signal = await signals.add(make_signal(fixture))
            await assessments.add(
                RiskAssessment(
                    signal_id=signal.id,
                    trading_day_id=fixture.day.id,
                    risk_configuration_id=fixture.risk_config_id,
                    verdict=RiskVerdict.REJECTED,
                    reason_codes=codes,
                    evaluated_at=GENERATED_AT + timedelta(minutes=index),
                    correlation_id=signal.correlation_id,
                )
            )

        stale = await assessments.list_rejections_with_reason(
            RiskReasonCode.STALE_MARKET_DATA.value
        )
        assert len(stale) == 2

        spread = await assessments.list_rejections_with_reason(RiskReasonCode.SPREAD_TOO_WIDE.value)
        assert len(spread) == 1

        none_found = await assessments.list_rejections_with_reason(
            RiskReasonCode.EMERGENCY_STOP_ACTIVE.value
        )
        assert none_found == []

    async def test_every_evaluated_rule_is_stored_not_only_the_verdict(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """An approval must be as explainable as a refusal."""
        fixture = await build_fixture(db_session, settings)
        signal = await SignalRepository(db_session).add(make_signal(fixture))

        assessment = await RiskAssessmentRepository(db_session).add(
            RiskAssessment(
                signal_id=signal.id,
                trading_day_id=fixture.day.id,
                risk_configuration_id=fixture.risk_config_id,
                verdict=RiskVerdict.REJECTED,
                reason_codes=[RiskReasonCode.REWARD_RISK_TOO_LOW.value],
                evaluated_rules={
                    "R-02": {"verdict": "PASS", "riskQuote": "1.0855", "limitQuote": "1.0870"},
                    "R-14": {
                        "verdict": "FAIL",
                        "netRewardRisk": "1.42",
                        "minimum": "1.80",
                        "grossRewardRisk": "2.00",
                    },
                },
                explanation=(
                    "Refused: reward-to-risk is 1.42 net of fees and slippage, "
                    "below the required 1.80. Gross would have been 2.00."
                ),
                evaluated_at=GENERATED_AT,
                correlation_id=signal.correlation_id,
            )
        )
        db_session.expunge_all()

        loaded = await db_session.get(RiskAssessment, assessment.id)
        assert loaded is not None
        assert loaded.evaluated_rules["R-14"]["verdict"] == "FAIL"
        assert loaded.evaluated_rules["R-14"]["grossRewardRisk"] == "2.00"
        assert loaded.explanation is not None
        assert "net of fees" in loaded.explanation

    async def test_the_configuration_behind_the_verdict_is_recorded(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        signal = await SignalRepository(db_session).add(make_signal(fixture))
        assessment = await RiskAssessmentRepository(db_session).add(
            RiskAssessment(
                signal_id=signal.id,
                trading_day_id=fixture.day.id,
                risk_configuration_id=fixture.risk_config_id,
                verdict=RiskVerdict.APPROVED,
                reason_codes=[],
                evaluated_at=GENERATED_AT,
                correlation_id=signal.correlation_id,
            )
        )
        assert assessment.risk_configuration_id == fixture.risk_config_id


class TestDecisionChain:
    async def test_one_correlation_id_links_the_whole_decision(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        audit = AuditRepository(db_session)
        correlation_id = uuid.uuid4()

        signal = await SignalRepository(db_session).add(
            make_signal(fixture, correlation_id=correlation_id)
        )
        await audit.record(
            event_type="SIGNAL_GENERATED",
            aggregate_type="Signal",
            aggregate_id=signal.id,
            correlation_id=correlation_id,
        )

        assessment = await RiskAssessmentRepository(db_session).add(
            RiskAssessment(
                signal_id=signal.id,
                trading_day_id=fixture.day.id,
                risk_configuration_id=fixture.risk_config_id,
                verdict=RiskVerdict.REJECTED,
                reason_codes=[RiskReasonCode.STALE_MARKET_DATA.value],
                evaluated_at=GENERATED_AT,
                correlation_id=correlation_id,
            )
        )
        await apply_transition(
            entity=signal,
            machine=SIGNAL_MACHINE,
            target=SignalStatus.RISK_REJECTED,
            audit=audit,
            aggregate_type="Signal",
            event_type="SIGNAL_RISK_REJECTED",
            correlation_id=correlation_id,
            reason=RiskReasonCode.STALE_MARKET_DATA.value,
        )

        chain = await audit.list_for_correlation(correlation_id)
        assert [event.event_type for event in chain] == [
            "SIGNAL_GENERATED",
            "SIGNAL_RISK_REJECTED",
        ]
        assert assessment.correlation_id == correlation_id
        assert signal.status is SignalStatus.RISK_REJECTED
