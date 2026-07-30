"""The risk engine against a real database.

What is verified here is the wiring the pure tests cannot reach: that the
verdict is written whatever it was, that the day is judged by the configuration
it was opened under, that the currency conversion happens, and that an ENTRY
order without an approval is refused by PostgreSQL rather than by a convention.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.clock import FixedClock
from app.domain.enums import (
    ExecutionVenue,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    RiskReasonCode,
    RiskVerdict,
    SignalStatus,
    Timeframe,
    TradingDayStatus,
)
from app.domain.position_sizing import SizingRequest, SizingResult, size_position
from app.domain.risk.context import MarketState, SystemState
from app.domain.risk.economics import TradingCosts
from app.domain.symbol_filters import (
    LotSizeFilter,
    NotionalFilter,
    PriceFilter,
    SymbolFilters,
)
from app.persistence.models import (
    AuditEvent,
    Order,
    RiskAssessment,
    Signal,
    Strategy,
    StrategyVersion,
    TradingDay,
    TradingPair,
)
from app.persistence.repositories import ConfigurationRepository, TradingDayRepository
from app.persistence.seed import seed
from app.risk.assessor import AssessmentResult, RiskAssessor, approved_assessment_id
from app.risk.mapping import limits_from_configuration

pytestmark = pytest.mark.integration

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
FUNDING_RATE = Decimal("4.60")
VENUE = ExecutionVenue.PAPER

BTC_FILTERS = SymbolFilters(
    symbol="BTCUSDT",
    price=PriceFilter(
        min_price=Decimal("0.01"), max_price=Decimal("1000000"), tick_size=Decimal("0.01")
    ),
    lot_size=LotSizeFilter(
        min_quantity=Decimal("0.00001"),
        max_quantity=Decimal("9000"),
        step_size=Decimal("0.00001"),
    ),
    notional=NotionalFilter(
        min_notional=Decimal("5"), max_notional=Decimal("9000000"), applies_to_market_orders=True
    ),
)

#: Binance spot with the BNB discount (decision OD-16): 0.075% per side.
COSTS = TradingCosts(fee_rate_per_side=Decimal("0.00075"))

#: Every gate calibrated. The shipped configuration deliberately is not, and a
#: test below covers that case on its own.
CALIBRATION = {
    "max_candle_age_seconds": 1800,
    "max_signal_age_seconds": 300,
    "max_spread_bps": Decimal("5"),
    "min_order_book_depth_quote": Decimal("10000"),
    "min_atr_percent": Decimal("0.15"),
    "max_atr_percent": Decimal("2.00"),
    "min_reward_risk_ratio": Decimal("1.8"),
    "max_estimated_slippage_bps": Decimal("10"),
    "max_clock_drift_ms": 1000,
}

HEALTHY_MARKET = MarketState(
    candle_age=timedelta(seconds=60),
    spread_bps=Decimal("1.5"),
    order_book_depth_quote=Decimal("250000"),
    atr_percent=Decimal("0.60"),
    estimated_slippage_bps=Decimal("2"),
    clock_drift_ms=Decimal("120"),
)


class Fixture:
    def __init__(self, day: TradingDay, pair: TradingPair, version: StrategyVersion) -> None:
        self.day = day
        self.pair = pair
        self.version = version


async def build(
    db_session: AsyncSession, settings: Settings, *, calibrated: bool = True
) -> Fixture:
    await seed(db_session, settings)
    configurations = ConfigurationRepository(db_session)
    risk = await configurations.get_active_risk_configuration()
    trading = await configurations.get_active_trading_configuration()
    assert risk is not None and trading is not None

    if calibrated:
        for name, value in CALIBRATION.items():
            setattr(risk, name, value)
        await db_session.flush()

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

    strategy = Strategy(name="trend_pullback_v1", is_enabled=True)
    db_session.add(strategy)
    await db_session.flush()
    version = StrategyVersion(strategy_id=strategy.id, version=1, is_active=True, parameters={})
    db_session.add(version)
    await db_session.flush()
    return Fixture(day, pair, version)


def make_signal(fixture: Fixture, *, generated_at: datetime = NOW) -> Signal:
    return Signal(
        trading_day_id=fixture.day.id,
        trading_pair_id=fixture.pair.id,
        strategy_version_id=fixture.version.id,
        status=SignalStatus.GENERATED,
        side=OrderSide.BUY,
        timeframe=Timeframe.M15,
        generated_at=generated_at,
        reference_price=Decimal("65000.00"),
        stop_loss_price=Decimal("64350.00"),
        take_profit_price=Decimal("66625.00"),
        correlation_id=uuid.uuid4(),
    )


def sizing_for(signal: Signal, budget: Decimal = Decimal("5.00")) -> SizingResult:
    return size_position(
        SizingRequest(
            side=signal.side,
            reference_price=signal.reference_price,
            stop_loss_price=signal.stop_loss_price,
            take_profit_price=signal.take_profit_price,
            risk_budget_reporting=budget,
            funding_rate=FUNDING_RATE,
            filters=BTC_FILTERS,
        )
    )


async def assess(
    db_session: AsyncSession,
    fixture: Fixture,
    signal: Signal,
    **overrides: object,
) -> AssessmentResult:
    db_session.add(signal)
    await db_session.flush()
    kwargs: dict[str, object] = {
        "signal": signal,
        "day": fixture.day,
        "venue": VENUE,
        "costs": COSTS,
        "sizing": sizing_for(signal),
        "market": HEALTHY_MARKET,
        "system": SystemState(),
        "time_remaining_in_day": timedelta(hours=6),
    }
    kwargs.update(overrides)
    assessor = RiskAssessor(db_session, clock=FixedClock(NOW))
    return await assessor.assess(**kwargs)  # type: ignore[arg-type]


class TestApproval:
    async def test_a_clean_signal_is_approved_and_recorded(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        result = await assess(db_session, fixture, make_signal(fixture))

        assert result.is_approved
        assert result.assessment.verdict is RiskVerdict.APPROVED
        assert result.assessment.reason_codes == []
        assert result.assessment.approved_quantity is not None
        assert result.assessment.approved_risk_quote is not None

    async def test_the_signal_moves_to_risk_approved(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        signal = make_signal(fixture)
        await assess(db_session, fixture, signal)
        assert signal.status is SignalStatus.RISK_APPROVED

    async def test_every_rule_is_written_into_the_record(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Not only the ones that refused. An approval that cannot be explained
        is as useless as a refusal that cannot be."""
        fixture = await build(db_session, settings)
        result = await assess(db_session, fixture, make_signal(fixture))

        rules = result.assessment.evaluated_rules["rules"]
        assert len(rules) == 24
        recorded = {rule["rule_id"]: rule for rule in rules}
        assert recorded["R-02"]["inputs"]["risk_amount"]
        assert recorded["R-02"]["parameters"]["maximum_risk_per_trade"] == "5.00"

    async def test_the_correlation_id_links_the_signal_to_its_assessment(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """One query returns the whole decision, months later."""
        fixture = await build(db_session, settings)
        signal = make_signal(fixture)
        result = await assess(db_session, fixture, signal)
        assert result.assessment.correlation_id == signal.correlation_id


class TestRejection:
    async def test_a_rejection_is_recorded_not_omitted(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """ "No order was placed" and "an order was refused because the spread
        was 9 bps" are different facts, and only one can be learned from."""
        fixture = await build(db_session, settings)
        signal = make_signal(fixture)
        result = await assess(
            db_session, fixture, signal, system=SystemState(emergency_stop_active=True)
        )

        assert not result.is_approved
        assert RiskReasonCode.EMERGENCY_STOP_ACTIVE.value in result.assessment.reason_codes
        assert signal.status is SignalStatus.RISK_REJECTED

    async def test_a_rejection_carries_no_approved_quantity(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A quantity stored beside a rejection is a number somebody will
        eventually act on."""
        fixture = await build(db_session, settings)
        result = await assess(
            db_session,
            fixture,
            make_signal(fixture),
            system=SystemState(emergency_stop_active=True),
        )
        assert result.assessment.approved_quantity is None
        assert result.assessment.approved_risk_quote is None

    async def test_an_unsized_signal_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        result = await assess(db_session, fixture, make_signal(fixture), sizing=None)
        assert not result.is_approved
        assert RiskReasonCode.EXCHANGE_FILTER_VIOLATION.value in result.assessment.reason_codes

    async def test_the_transition_is_audited(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        signal = make_signal(fixture)
        await assess(db_session, fixture, signal, system=SystemState(emergency_stop_active=True))

        events = (
            (
                await db_session.execute(
                    select(AuditEvent).where(AuditEvent.aggregate_id == signal.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].previous_state == SignalStatus.GENERATED.value
        assert events[0].new_state == SignalStatus.RISK_REJECTED.value


class TestCurrencyConversion:
    async def test_a_quote_loss_is_compared_against_a_reporting_limit(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """The conversion that must not be skipped. A 9 USDT loss is 41.40 RON
        at 4.60, which is past the 40 RON floor; comparing 9 against 40 would
        pass a day that had already breached it."""
        fixture = await build(db_session, settings)
        fixture.day.realised_pnl_quote = Decimal("-9.00")
        await db_session.flush()

        result = await assess(db_session, fixture, make_signal(fixture))

        assert RiskReasonCode.DAILY_LOSS_LIMIT_REACHED.value in result.assessment.reason_codes

    async def test_a_loss_below_the_converted_limit_still_passes(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        fixture.day.realised_pnl_quote = Decimal("-8.00")  # 36.80 RON
        await db_session.flush()

        result = await assess(db_session, fixture, make_signal(fixture))

        assert RiskReasonCode.DAILY_LOSS_LIMIT_REACHED.value not in (result.assessment.reason_codes)

    async def test_an_unrealised_loss_passed_in_live_overrides_the_stored_one(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """The stored column is only as fresh as the last snapshot; R-03 on the
        conservative basis needs the mark now."""
        fixture = await build(db_session, settings)
        result = await assess(
            db_session,
            fixture,
            make_signal(fixture),
            unrealised_pnl_quote=Decimal("-9.00"),
        )
        assert RiskReasonCode.DAILY_LOSS_LIMIT_REACHED.value in result.assessment.reason_codes


class TestConfigurationVersioning:
    async def test_the_day_is_judged_by_the_configuration_it_was_opened_under(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A change at 14:00 must not rewrite what the rules were at 09:00."""
        fixture = await build(db_session, settings)
        original = await ConfigurationRepository(db_session).get_risk_configuration_by_id(
            fixture.day.risk_configuration_id
        )
        assert original is not None

        # A new, stricter configuration is activated after the day opened.
        original.is_active = False
        stricter = type(original)(
            version=99,
            is_active=True,
            reference_capital_ron=Decimal("1000.00"),
            reporting_currency="RON",
            session_target_percent=Decimal("2.00"),
            session_restart_threshold_percent=Decimal("4.00"),
            daily_maximum_loss_percent=Decimal("4.00"),
            maximum_risk_per_trade_percent=Decimal("0.01"),
            maximum_open_positions=1,
            maximum_trades_per_day=50,
            maximum_consecutive_losses=3,
            no_new_entry_minutes_before_day_end=30,
            **CALIBRATION,
        )
        db_session.add(stricter)
        await db_session.flush()

        result = await assess(db_session, fixture, make_signal(fixture))

        # Judged under the ORIGINAL 0.50%, so the trade still fits.
        assert result.is_approved
        assert result.assessment.risk_configuration_id == original.id

    async def test_the_limits_mirror_the_stored_configuration(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        configuration = await ConfigurationRepository(db_session).get_risk_configuration_by_id(
            fixture.day.risk_configuration_id
        )
        assert configuration is not None
        limits = limits_from_configuration(configuration)
        assert limits.reference_capital == Decimal("1000.00")
        assert limits.risk_per_trade_amount == Decimal("5.00")
        assert limits.daily_maximum_loss_amount == Decimal("40.00")


class TestUncalibratedConfigurationRefuses:
    async def test_the_shipped_configuration_cannot_trade_yet(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Eight gates are deliberately uncalibrated until Phase 6. Treating an
        unknown limit as no limit is how a protection becomes decorative."""
        fixture = await build(db_session, settings, calibrated=False)
        result = await assess(db_session, fixture, make_signal(fixture))

        assert not result.is_approved
        assert RiskReasonCode.RISK_CONFIGURATION_INCOMPLETE.value in (
            result.assessment.reason_codes
        )

    async def test_the_uncalibrated_gates_are_named_in_the_record(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings, calibrated=False)
        result = await assess(db_session, fixture, make_signal(fixture))

        rules = {rule["rule_id"]: rule for rule in result.assessment.evaluated_rules["rules"]}
        assert rules["R-11"]["status"] == "NOT_CALIBRATED"
        assert "max_spread_bps" in rules["R-11"]["parameters"]


class TestEntryOrdersRequireApproval:
    """AC-19, enforced by PostgreSQL rather than by a convention."""

    async def test_an_entry_order_without_an_assessment_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        db_session.add(
            Order(
                trading_day_id=fixture.day.id,
                trading_pair_id=fixture.pair.id,
                risk_assessment_id=None,
                venue=VENUE,
                purpose=OrderPurpose.ENTRY,
                status=OrderStatus.INTENT_RECORDED,
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                requested_quantity=Decimal("0.001"),
                requested_price=Decimal("65000"),
                client_order_id=f"test-{uuid.uuid4().hex[:16]}",
                correlation_id=uuid.uuid4(),
                intent_recorded_at=NOW,
            )
        )
        # Named, so the test cannot pass because some unrelated NOT NULL fired.
        with pytest.raises(IntegrityError, match="entry_requires_risk_approval"):
            await db_session.flush()

    async def test_an_approved_assessment_lets_the_entry_through(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        signal = make_signal(fixture)
        result = await assess(db_session, fixture, signal)
        assessment_id = approved_assessment_id(result)
        assert assessment_id is not None

        db_session.add(
            Order(
                trading_day_id=fixture.day.id,
                trading_pair_id=fixture.pair.id,
                signal_id=signal.id,
                risk_assessment_id=assessment_id,
                venue=VENUE,
                purpose=OrderPurpose.ENTRY,
                status=OrderStatus.INTENT_RECORDED,
                side=OrderSide.BUY,
                type=OrderType.LIMIT,
                requested_quantity=result.assessment.approved_quantity or Decimal("0.001"),
                requested_price=Decimal("65000"),
                client_order_id=f"test-{uuid.uuid4().hex[:16]}",
                correlation_id=signal.correlation_id,
                intent_recorded_at=NOW,
            )
        )
        await db_session.flush()

    async def test_a_rejected_assessment_yields_no_id_to_use(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """What stops an unapproved trade is this returning None, not a
        convention anyone has to remember."""
        fixture = await build(db_session, settings)
        result = await assess(
            db_session,
            fixture,
            make_signal(fixture),
            system=SystemState(emergency_stop_active=True),
        )
        assert approved_assessment_id(result) is None

    async def test_an_exit_order_needs_no_approval(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A halted day must still be able to protect an open position."""
        fixture = await build(db_session, settings)
        db_session.add(
            Order(
                trading_day_id=fixture.day.id,
                trading_pair_id=fixture.pair.id,
                risk_assessment_id=None,
                venue=VENUE,
                purpose=OrderPurpose.STOP_LOSS,
                status=OrderStatus.INTENT_RECORDED,
                side=OrderSide.SELL,
                type=OrderType.LIMIT,
                requested_quantity=Decimal("0.001"),
                requested_price=Decimal("64000"),
                client_order_id=f"test-{uuid.uuid4().hex[:16]}",
                correlation_id=uuid.uuid4(),
                intent_recorded_at=NOW,
            )
        )
        await db_session.flush()


class TestDayState:
    async def test_open_positions_are_counted_from_the_database(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        result = await assess(db_session, fixture, make_signal(fixture))
        rules = {rule["rule_id"]: rule for rule in result.assessment.evaluated_rules["rules"]}
        assert rules["R-04"]["inputs"]["open_positions"] == "0"

    async def test_the_trade_count_comes_from_the_day_row(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        fixture.day.trade_count = 50
        await db_session.flush()

        result = await assess(db_session, fixture, make_signal(fixture))
        assert RiskReasonCode.MAX_TRADES_PER_DAY_REACHED.value in (result.assessment.reason_codes)

    async def test_the_signal_age_is_measured_against_the_injected_clock(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build(db_session, settings)
        stale = make_signal(fixture, generated_at=NOW - timedelta(minutes=10))
        result = await assess(db_session, fixture, stale)
        assert RiskReasonCode.SIGNAL_EXPIRED.value in result.assessment.reason_codes


class TestPersistedShape:
    async def test_the_assessment_is_queryable_by_reason_code(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """The GIN index exists so "every refusal caused by X last month" is one
        indexed query rather than a scan."""
        fixture = await build(db_session, settings)
        await assess(
            db_session,
            fixture,
            make_signal(fixture),
            system=SystemState(emergency_stop_active=True),
        )
        found = (
            (
                await db_session.execute(
                    select(RiskAssessment).where(
                        RiskAssessment.reason_codes.contains(
                            [RiskReasonCode.EMERGENCY_STOP_ACTIVE.value]
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(found) == 1
