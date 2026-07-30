"""Order, fill, position and trade tests.

These cover the invariants that cost real money when they are wrong.
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
from app.domain.enums import (
    AuditActor,
    ExecutionVenue,
    ExitReason,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    RiskReasonCode,
    RiskVerdict,
    TimeInForce,
    TradingDayStatus,
)
from app.domain.errors import InvalidTransitionError
from app.domain.state_machines import ORDER_MACHINE
from app.persistence.models import (
    Order,
    OrderFill,
    Position,
    RiskAssessment,
    Trade,
    TradingDay,
    TradingPair,
)
from app.persistence.models.snapshots import BalanceSnapshot, PnLSnapshot
from app.persistence.repositories import (
    AuditRepository,
    ConfigurationRepository,
    OrderRepository,
    PositionRepository,
    SnapshotRepository,
    TradeRepository,
    TradingDayRepository,
)
from app.persistence.seed import seed
from app.persistence.state_transitions import apply_transition

pytestmark = pytest.mark.integration

FUNDING_RATE = Decimal("4.60")
OPENED_AT = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
VENUE = ExecutionVenue.PAPER


class Fixture:
    def __init__(
        self,
        day: TradingDay,
        pair: TradingPair,
        risk_config_id: uuid.UUID,
        approval: RiskAssessment,
    ) -> None:
        self.day = day
        self.pair = pair
        self.risk_config_id = risk_config_id
        self.approval = approval


async def build_fixture(
    db_session: AsyncSession, settings: Settings, trading_date: date = date(2026, 7, 28)
) -> Fixture:
    await seed(db_session, settings)
    configurations = ConfigurationRepository(db_session)
    risk = await configurations.get_active_risk_configuration()
    trading = await configurations.get_active_trading_configuration()
    assert risk is not None and trading is not None

    day = await TradingDayRepository(db_session).add(
        TradingDay(
            trading_date=trading_date,
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

    approval = RiskAssessment(
        trading_day_id=day.id,
        risk_configuration_id=risk.id,
        verdict=RiskVerdict.APPROVED,
        reason_codes=[],
        approved_quantity=Decimal("0.00167000"),
        approved_risk_quote=Decimal("1.08550000"),
        evaluated_at=OPENED_AT,
        correlation_id=uuid.uuid4(),
    )
    db_session.add(approval)
    await db_session.flush()

    return Fixture(day, pair, risk.id, approval)


def make_order(fixture: Fixture, **overrides: object) -> Order:
    values: dict[str, object] = {
        "client_order_id": f"trader-{uuid.uuid4().hex[:16]}",
        "venue": VENUE,
        "trading_day_id": fixture.day.id,
        "trading_pair_id": fixture.pair.id,
        "risk_assessment_id": fixture.approval.id,
        "purpose": OrderPurpose.ENTRY,
        "side": OrderSide.BUY,
        "type": OrderType.LIMIT,
        "time_in_force": TimeInForce.GTC,
        "requested_quantity": Decimal("0.00167000"),
        "requested_price": Decimal("65000.00"),
        "intent_recorded_at": OPENED_AT,
        "correlation_id": uuid.uuid4(),
    }
    values.update(overrides)
    return Order(**values)


class TestOrderIdempotency:
    async def test_the_client_order_id_is_unique(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """The idempotency key. Two orders sharing it would defeat reconciliation."""
        fixture = await build_fixture(db_session, settings)
        first = await OrderRepository(db_session).add(make_order(fixture))

        db_session.add(make_order(fixture, client_order_id=first.client_order_id))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_an_order_is_findable_by_the_id_we_generated(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """After a timeout, reconciliation asks the exchange about THIS id."""
        fixture = await build_fixture(db_session, settings)
        orders = OrderRepository(db_session)
        created = await orders.add(make_order(fixture))

        found = await orders.get_by_client_order_id(created.client_order_id)
        assert found is not None
        assert found.id == created.id

    async def test_the_same_exchange_order_id_cannot_appear_twice_on_a_venue(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        orders = OrderRepository(db_session)
        await orders.add(make_order(fixture, exchange_order_id="EX-1"))

        db_session.add(make_order(fixture, exchange_order_id="EX-1"))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_many_orders_may_have_no_exchange_id_yet(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """The unique index is partial: NULL means "not acknowledged yet"."""
        fixture = await build_fixture(db_session, settings)
        orders = OrderRepository(db_session)
        await orders.add(make_order(fixture))
        await orders.add(make_order(fixture))
        await db_session.flush()


class TestEntryRequiresRiskApproval:
    """AC-19 as a database constraint, not a convention."""

    async def test_an_entry_without_a_risk_assessment_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        db_session.add(make_order(fixture, risk_assessment_id=None))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_a_protective_exit_needs_no_approval(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A halted day must still be able to protect an open position."""
        fixture = await build_fixture(db_session, settings)
        await OrderRepository(db_session).add(
            make_order(
                fixture,
                purpose=OrderPurpose.STOP_LOSS,
                side=OrderSide.SELL,
                risk_assessment_id=None,
            )
        )
        await db_session.flush()

    async def test_only_an_entry_opens_exposure(self) -> None:
        opening = {purpose for purpose in OrderPurpose if purpose.opens_exposure}
        assert opening == {OrderPurpose.ENTRY}


class TestOrderShapeConstraints:
    async def test_a_limit_order_without_a_price_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        db_session.add(make_order(fixture, type=OrderType.LIMIT, requested_price=None))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_a_market_order_needs_no_price(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        await OrderRepository(db_session).add(
            make_order(fixture, type=OrderType.MARKET, requested_price=None, time_in_force=None)
        )
        await db_session.flush()

    async def test_filling_more_than_requested_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        db_session.add(
            make_order(
                fixture,
                requested_quantity=Decimal("0.001"),
                filled_quantity=Decimal("0.002"),
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestFillDeduplication:
    async def test_the_same_exchange_fill_cannot_be_recorded_twice(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A reconnecting stream replays events; a REST fallback repeats them."""
        fixture = await build_fixture(db_session, settings)
        orders = OrderRepository(db_session)
        order = await orders.add(make_order(fixture))

        await orders.add_fill(
            OrderFill(
                order_id=order.id,
                exchange_trade_id="TRADE-42",
                quantity=Decimal("0.001"),
                price=Decimal("65000.00"),
                filled_at=OPENED_AT,
            )
        )
        db_session.add(
            OrderFill(
                order_id=order.id,
                exchange_trade_id="TRADE-42",
                quantity=Decimal("0.001"),
                price=Decimal("65000.00"),
                filled_at=OPENED_AT,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_partial_fills_with_different_ids_are_allowed(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        orders = OrderRepository(db_session)
        order = await orders.add(make_order(fixture))

        for index, trade_id in enumerate(["TRADE-1", "TRADE-2"]):
            await orders.add_fill(
                OrderFill(
                    order_id=order.id,
                    exchange_trade_id=trade_id,
                    quantity=Decimal("0.0008"),
                    price=Decimal("65000.00"),
                    filled_at=OPENED_AT + timedelta(seconds=index),
                )
            )
        db_session.expunge_all()

        loaded = await db_session.get(Order, order.id)
        assert loaded is not None
        result = await db_session.execute(select(OrderFill).where(OrderFill.order_id == loaded.id))
        assert len(result.scalars().all()) == 2


class TestUncertainOrderState:
    """AC-15: an uncertain order triggers reconciliation, never a blind retry."""

    async def test_a_timed_out_order_can_only_go_to_reconciling(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        order = await OrderRepository(db_session).add(make_order(fixture))
        audit = AuditRepository(db_session)

        for target in (OrderStatus.SUBMITTING, OrderStatus.UNKNOWN):
            await apply_transition(
                entity=order,
                machine=ORDER_MACHINE,
                target=target,
                audit=audit,
                aggregate_type="Order",
                event_type="ORDER_TRANSITION",
            )

        with pytest.raises(InvalidTransitionError):
            await apply_transition(
                entity=order,
                machine=ORDER_MACHINE,
                target=OrderStatus.SUBMITTING,
                audit=audit,
                aggregate_type="Order",
                event_type="ORDER_RESUBMITTED",
            )

        assert order.status is OrderStatus.UNKNOWN

    async def test_orders_of_unknown_state_are_discoverable(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Until this list is empty, the system does not know its own exposure."""
        fixture = await build_fixture(db_session, settings)
        orders = OrderRepository(db_session)
        order = await orders.add(make_order(fixture, status=OrderStatus.UNKNOWN))
        await orders.add(make_order(fixture, status=OrderStatus.FILLED))
        await db_session.flush()

        pending = await orders.list_needing_reconciliation()
        assert [o.id for o in pending] == [order.id]


class TestPositionSlots:
    async def test_every_non_final_status_occupies_a_slot(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A closing position has not released the slot; a desynced one owns it."""
        fixture = await build_fixture(db_session, settings)
        positions = PositionRepository(db_session)

        for status in (
            PositionStatus.OPENING,
            PositionStatus.OPEN,
            PositionStatus.CLOSING,
            PositionStatus.DESYNCED,
            PositionStatus.CLOSED,
            PositionStatus.ABANDONED,
        ):
            await positions.add(
                Position(
                    trading_pair_id=fixture.pair.id,
                    venue=VENUE,
                    opened_trading_day_id=fixture.day.id,
                    status=status,
                    side=OrderSide.BUY,
                    quantity=Decimal("0.001"),
                    entry_price=Decimal("65000.00"),
                    opened_at=OPENED_AT,
                    correlation_id=uuid.uuid4(),
                )
            )
        await db_session.flush()

        assert await positions.count_occupying_slots(VENUE) == 4

    async def test_a_desynced_position_is_discoverable(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        positions = PositionRepository(db_session)
        await positions.add(
            Position(
                trading_pair_id=fixture.pair.id,
                venue=VENUE,
                opened_trading_day_id=fixture.day.id,
                status=PositionStatus.DESYNCED,
                side=OrderSide.BUY,
                quantity=Decimal("0.001"),
                entry_price=Decimal("65000.00"),
                opened_at=OPENED_AT,
                desync_detail="Exchange reports 0.0005, local state says 0.001.",
                correlation_id=uuid.uuid4(),
            )
        )
        await db_session.flush()

        desynced = await positions.list_desynced()
        assert len(desynced) == 1
        assert desynced[0].desync_detail is not None


async def make_position(db_session: AsyncSession, fixture: Fixture) -> Position:
    return await PositionRepository(db_session).add(
        Position(
            trading_pair_id=fixture.pair.id,
            venue=VENUE,
            opened_trading_day_id=fixture.day.id,
            status=PositionStatus.CLOSED,
            side=OrderSide.BUY,
            quantity=Decimal("0.001"),
            entry_price=Decimal("65000.00"),
            opened_at=OPENED_AT,
            correlation_id=uuid.uuid4(),
        )
    )


def make_trade(fixture: Fixture, position: Position, **overrides: object) -> Trade:
    values: dict[str, object] = {
        "position_id": position.id,
        "trading_pair_id": fixture.pair.id,
        "venue": VENUE,
        "opened_trading_day_id": fixture.day.id,
        "closed_trading_day_id": fixture.day.id,
        "quantity": Decimal("0.001"),
        "entry_price": Decimal("65000.00"),
        "exit_price": Decimal("66000.00"),
        "gross_pnl_quote": Decimal("1.00000000"),
        "fees_quote": Decimal("0.13000000"),
        "slippage_quote": Decimal("0.02000000"),
        "net_pnl_quote": Decimal("0.85000000"),
        "is_win": True,
        "exit_reason": ExitReason.TAKE_PROFIT,
        "opened_at": OPENED_AT,
        "closed_at": OPENED_AT + timedelta(hours=2),
        "correlation_id": uuid.uuid4(),
    }
    values.update(overrides)
    return Trade(**values)


class TestTradeAccounting:
    async def test_a_consistent_trade_is_accepted(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        position = await make_position(db_session, fixture)
        trade = await TradeRepository(db_session).add(make_trade(fixture, position))
        assert trade.net_pnl_quote == Decimal("0.85000000")

    async def test_net_must_equal_gross_minus_costs(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """If these disagree, every report built on the ledger is wrong."""
        fixture = await build_fixture(db_session, settings)
        position = await make_position(db_session, fixture)
        db_session.add(make_trade(fixture, position, net_pnl_quote=Decimal("1.00000000")))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_the_win_flag_must_match_the_net_result(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A win is defined by the NET result, after fees and slippage."""
        fixture = await build_fixture(db_session, settings)
        position = await make_position(db_session, fixture)
        db_session.add(
            make_trade(
                fixture,
                position,
                gross_pnl_quote=Decimal("0.10000000"),
                fees_quote=Decimal("0.13000000"),
                slippage_quote=Decimal("0.02000000"),
                net_pnl_quote=Decimal("-0.05000000"),
                is_win=True,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_a_gross_win_can_be_a_net_loss(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Costs decide. This is exactly the case the 50-trades-a-day limit invites."""
        fixture = await build_fixture(db_session, settings)
        position = await make_position(db_session, fixture)
        trade = await TradeRepository(db_session).add(
            make_trade(
                fixture,
                position,
                gross_pnl_quote=Decimal("0.10000000"),
                fees_quote=Decimal("0.13000000"),
                slippage_quote=Decimal("0.02000000"),
                net_pnl_quote=Decimal("-0.05000000"),
                is_win=False,
            )
        )
        assert trade.gross_pnl_quote > 0
        assert trade.net_pnl_quote < 0
        assert trade.is_win is False

    async def test_one_trade_per_position(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        position = await make_position(db_session, fixture)
        await TradeRepository(db_session).add(make_trade(fixture, position))

        db_session.add(make_trade(fixture, position))
        with pytest.raises(IntegrityError):
            await db_session.flush()


class TestDayBoundaryAttribution:
    """AC-26: a position opened at 23:40 and closed at 01:15."""

    async def test_the_count_belongs_to_the_opening_day_and_the_pnl_to_the_closing_day(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings, trading_date=date(2026, 7, 28))
        days = TradingDayRepository(db_session)
        next_day = await days.add(
            TradingDay(
                trading_date=date(2026, 7, 29),
                timezone=fixture.day.timezone,
                status=TradingDayStatus.ACTIVE,
                risk_configuration_id=fixture.day.risk_configuration_id,
                trading_configuration_id=fixture.day.trading_configuration_id,
                reporting_currency="RON",
                quote_currency="USDT",
                reference_capital_ron=Decimal("1000.00"),
                funding_rate_ron_per_quote=FUNDING_RATE,
            )
        )

        position = await make_position(db_session, fixture)
        position.closed_trading_day_id = next_day.id
        trades = TradeRepository(db_session)
        await trades.add(
            make_trade(
                fixture,
                position,
                opened_trading_day_id=fixture.day.id,
                closed_trading_day_id=next_day.id,
                opened_at=datetime(2026, 7, 28, 20, 40, tzinfo=UTC),
                closed_at=datetime(2026, 7, 28, 22, 15, tzinfo=UTC),
            )
        )
        await db_session.flush()

        # R-05 counts new entries, so the trade belongs to the day it opened.
        assert len(await trades.list_opened_on_day(fixture.day.id)) == 1
        assert len(await trades.list_opened_on_day(next_day.id)) == 0

        # Realised P&L lands on the day the trade closed.
        assert len(await trades.list_closed_on_day(fixture.day.id)) == 0
        assert len(await trades.list_closed_on_day(next_day.id)) == 1


class TestConsecutiveLosses:
    """Rule R-06. A losing streak does not reset because the clock hit midnight."""

    async def test_losses_are_counted_until_the_last_win(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        trades = TradeRepository(db_session)

        # Oldest first: win, loss, loss.
        for index, is_win in enumerate([True, False, False]):
            position = await make_position(db_session, fixture)
            net = Decimal("0.85000000") if is_win else Decimal("-1.15000000")
            gross = net + Decimal("0.13000000") + Decimal("0.02000000")
            await trades.add(
                make_trade(
                    fixture,
                    position,
                    gross_pnl_quote=gross,
                    net_pnl_quote=net,
                    is_win=is_win,
                    exit_reason=ExitReason.TAKE_PROFIT if is_win else ExitReason.STOP_LOSS,
                    closed_at=OPENED_AT + timedelta(hours=index + 1),
                )
            )
        await db_session.flush()

        assert await trades.count_consecutive_losses(VENUE) == 2

    async def test_a_win_resets_the_streak(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        trades = TradeRepository(db_session)

        for index, is_win in enumerate([False, False, True]):
            position = await make_position(db_session, fixture)
            net = Decimal("0.85000000") if is_win else Decimal("-1.15000000")
            gross = net + Decimal("0.13000000") + Decimal("0.02000000")
            await trades.add(
                make_trade(
                    fixture,
                    position,
                    gross_pnl_quote=gross,
                    net_pnl_quote=net,
                    is_win=is_win,
                    exit_reason=ExitReason.TAKE_PROFIT if is_win else ExitReason.STOP_LOSS,
                    closed_at=OPENED_AT + timedelta(hours=index + 1),
                )
            )
        await db_session.flush()

        assert await trades.count_consecutive_losses(VENUE) == 0


class TestSnapshots:
    async def test_a_balance_whose_parts_do_not_add_up_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A wrong balance is how an order gets sized against money that is not there."""
        fixture = await build_fixture(db_session, settings)
        db_session.add(
            BalanceSnapshot(
                taken_at=OPENED_AT,
                venue=VENUE,
                source="EXCHANGE",
                asset="USDT",
                free_amount=Decimal("100.00"),
                locked_amount=Decimal("20.00"),
                total_amount=Decimal("110.00"),
                trading_day_id=fixture.day.id,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_the_latest_balance_wins(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        snapshots = SnapshotRepository(db_session)

        for index, total in enumerate([Decimal("100.00"), Decimal("120.00")]):
            await snapshots.add_balance(
                BalanceSnapshot(
                    taken_at=OPENED_AT + timedelta(minutes=index),
                    venue=VENUE,
                    source="EXCHANGE",
                    asset="USDT",
                    free_amount=total,
                    locked_amount=Decimal("0.00"),
                    total_amount=total,
                    trading_day_id=fixture.day.id,
                )
            )
        await db_session.flush()

        latest = await snapshots.latest_balance(VENUE, "USDT")
        assert latest is not None
        assert latest.total_amount == Decimal("120.00")

    async def test_a_pnl_snapshot_must_use_the_conservative_basis(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Rule R-26: net is realised plus unrealised, never realised alone."""
        fixture = await build_fixture(db_session, settings)
        db_session.add(
            PnLSnapshot(
                trading_day_id=fixture.day.id,
                taken_at=OPENED_AT,
                realised_pnl_quote=Decimal("2.00"),
                unrealised_pnl_quote=Decimal("-9.00"),
                net_pnl_quote=Decimal("2.00"),
                funding_rate_ron_per_quote=FUNDING_RATE,
                net_pnl_ron=Decimal("9.20"),
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_a_consistent_pnl_snapshot_is_accepted(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        snapshot = await SnapshotRepository(db_session).add_pnl(
            PnLSnapshot(
                trading_day_id=fixture.day.id,
                taken_at=OPENED_AT,
                realised_pnl_quote=Decimal("2.00"),
                unrealised_pnl_quote=Decimal("-9.00"),
                net_pnl_quote=Decimal("-7.00"),
                open_position_count=1,
                funding_rate_ron_per_quote=FUNDING_RATE,
                net_pnl_ron=Decimal("-32.20"),
            )
        )
        assert snapshot.net_pnl_quote == Decimal("-7.00")
        # -7.00 USDT at 4.60 RON per USDT is -32.20 RON, still inside the
        # -40 RON daily floor.
        assert snapshot.net_pnl_ron == Decimal("-32.20")


class TestPaperAndLiveNeverMix:
    async def test_the_venue_is_recorded_on_every_execution_row(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A paper fill must never be mistakable for a real one, ever."""
        fixture = await build_fixture(db_session, settings)
        order = await OrderRepository(db_session).add(make_order(fixture, venue=VENUE))
        position = await make_position(db_session, fixture)
        trade = await TradeRepository(db_session).add(make_trade(fixture, position))

        assert order.venue is ExecutionVenue.PAPER
        assert position.venue is ExecutionVenue.PAPER
        assert trade.venue is ExecutionVenue.PAPER

    async def test_position_slots_are_counted_per_venue(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        fixture = await build_fixture(db_session, settings)
        positions = PositionRepository(db_session)

        for venue in (ExecutionVenue.PAPER, ExecutionVenue.BACKTEST):
            await positions.add(
                Position(
                    trading_pair_id=fixture.pair.id,
                    venue=venue,
                    opened_trading_day_id=fixture.day.id,
                    status=PositionStatus.OPEN,
                    side=OrderSide.BUY,
                    quantity=Decimal("0.001"),
                    entry_price=Decimal("65000.00"),
                    opened_at=OPENED_AT,
                    correlation_id=uuid.uuid4(),
                )
            )
        await db_session.flush()

        assert await positions.count_occupying_slots(ExecutionVenue.PAPER) == 1
        assert await positions.count_occupying_slots(ExecutionVenue.LIVE) == 0


class TestFullDecisionChain:
    async def test_an_order_records_the_approval_that_authorised_it(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Signal -> RiskAssessment -> Order, reconstructable from the row."""
        fixture = await build_fixture(db_session, settings)
        audit = AuditRepository(db_session)
        correlation_id = fixture.approval.correlation_id

        order = await OrderRepository(db_session).add(
            make_order(fixture, correlation_id=correlation_id)
        )
        await apply_transition(
            entity=order,
            machine=ORDER_MACHINE,
            target=OrderStatus.SUBMITTING,
            audit=audit,
            aggregate_type="Order",
            event_type="ORDER_SUBMITTING",
            actor=AuditActor.SYSTEM,
            correlation_id=correlation_id,
            reason=RiskReasonCode.SESSION_RESTART_ELIGIBLE.value,
        )

        assert order.risk_assessment_id == fixture.approval.id
        chain = await audit.list_for_correlation(correlation_id)
        assert [event.event_type for event in chain] == ["ORDER_SUBMITTING"]
