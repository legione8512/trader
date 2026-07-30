"""Orders, fills, positions and trades.

This is the part of the schema where a mistake costs real money, so three
invariants are enforced by the database rather than by convention:

* an ``ENTRY`` order cannot exist without the risk approval that authorised it;
* the same exchange fill cannot be recorded twice, however many times a
  reconnecting stream replays it;
* a trade's net result must equal gross minus fees minus slippage.

Money is in the exchange quote currency, as everywhere else in the ledger.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import (
    ExecutionVenue,
    ExitReason,
    OrderPurpose,
    OrderSide,
    OrderStatus,
    OrderType,
    PositionStatus,
    TimeInForce,
)
from app.persistence.base import Base
from app.persistence.mixins import CreatedAtMixin, TimestampMixin, UuidPrimaryKeyMixin
from app.persistence.types import (
    UtcDateTime,
    currency_code,
    enum_column,
    monetary,
    price,
    quantity,
)

ZERO = Decimal(0)


class Order(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One order, from local intent to final exchange state."""

    __tablename__ = "order"
    __table_args__ = (
        Index("ix_order_status", "status"),
        Index("ix_order_trading_day_id", "trading_day_id"),
        Index("ix_order_correlation_id", "correlation_id"),
        # The exchange's own id is unique per venue, and NULL until the exchange
        # answers. A partial unique index is what turns a duplicate
        # acknowledgement into an error instead of a second order.
        Index(
            "uq_order_exchange_order_id",
            "venue",
            "exchange_order_id",
            unique=True,
            postgresql_where=text("exchange_order_id IS NOT NULL"),
        ),
        CheckConstraint("requested_quantity > 0", name="requested_quantity_positive"),
        CheckConstraint("filled_quantity >= 0", name="filled_quantity_non_negative"),
        CheckConstraint("filled_quantity <= requested_quantity", name="filled_not_above_requested"),
        CheckConstraint(
            "requested_price IS NULL OR requested_price > 0", name="requested_price_positive"
        ),
        CheckConstraint("fee_amount >= 0", name="fee_non_negative"),
        # A limit order without a price is not an order.
        CheckConstraint(
            "type <> 'LIMIT' OR requested_price IS NOT NULL", name="limit_order_has_price"
        ),
        # AC-19 as a database constraint: nothing can open exposure without the
        # risk assessment that approved it. A strategy cannot reach the exchange
        # by going around the risk engine, because the row would not insert.
        CheckConstraint(
            "purpose <> 'ENTRY' OR risk_assessment_id IS NOT NULL",
            name="entry_requires_risk_approval",
        ),
        CheckConstraint(
            "reconciliation_attempts >= 0", name="reconciliation_attempts_non_negative"
        ),
    )

    #: Generated locally BEFORE submission. This is the idempotency key: after a
    #: timeout, reconciliation asks the exchange about this id rather than
    #: sending a second order.
    client_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    exchange_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    #: PAPER, LIVE or BACKTEST. Recorded on the row so a simulated fill can
    #: never be mistaken for a real one, including in a report read years later.
    venue: Mapped[ExecutionVenue] = mapped_column(
        enum_column(ExecutionVenue, name="execution_venue"), nullable=False
    )

    trading_day_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("trading_day.id"), nullable=False)
    trading_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_session.id"), nullable=True
    )
    trading_pair_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_pair.id"), nullable=False
    )
    #: NULL for exits: a stop-loss is not caused by a signal.
    signal_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("signal.id"), nullable=True)
    #: The approval that authorised this order. Mandatory for an ENTRY.
    risk_assessment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("risk_assessment.id"), nullable=True
    )
    position_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("position.id"), nullable=True, index=True
    )

    purpose: Mapped[OrderPurpose] = mapped_column(
        enum_column(OrderPurpose, name="order_purpose"), nullable=False
    )
    side: Mapped[OrderSide] = mapped_column(
        enum_column(OrderSide, name="order_side"), nullable=False
    )
    type: Mapped[OrderType] = mapped_column(
        enum_column(OrderType, name="order_type"), nullable=False
    )
    time_in_force: Mapped[TimeInForce | None] = mapped_column(
        enum_column(TimeInForce, name="time_in_force"), nullable=True
    )
    status: Mapped[OrderStatus] = mapped_column(
        enum_column(OrderStatus, name="order_status"),
        nullable=False,
        default=OrderStatus.INTENT_RECORDED,
    )

    requested_quantity: Mapped[Decimal] = mapped_column(quantity(), nullable=False)
    requested_price: Mapped[Decimal | None] = mapped_column(price(), nullable=True)
    filled_quantity: Mapped[Decimal] = mapped_column(quantity(), nullable=False, default=ZERO)
    average_fill_price: Mapped[Decimal | None] = mapped_column(price(), nullable=True)
    fee_amount: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    fee_currency: Mapped[str | None] = mapped_column(currency_code(), nullable=True)

    #: Written BEFORE anything is sent, so a crash mid-flight is recoverable.
    intent_recorded_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    finalised_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    reject_reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: How many times reconciliation has asked the exchange about this order.
    #: Counting attempts, never resubmissions.
    reconciliation_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_reconciled_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: Request and response as sent and received, minus credentials. Both pass
    #: through the secret masker: an exchange response can quote a signed URL.
    exchange_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    exchange_response: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    fills: Mapped[list[OrderFill]] = relationship(
        back_populates="order",
        cascade="all, delete-orphan",
        order_by="OrderFill.filled_at",
    )

    @property
    def remaining_quantity(self) -> Decimal:
        return self.requested_quantity - self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.status in {
            OrderStatus.FILLED,
            OrderStatus.CANCELED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
            OrderStatus.UNRESOLVED,
        }

    def __repr__(self) -> str:
        return f"<Order {self.client_order_id} {self.status}>"


class OrderFill(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """One execution against an order. Append-only."""

    __tablename__ = "order_fill"
    __table_args__ = (
        # A reconnecting stream replays events, and a REST fallback returns the
        # same fills the WebSocket already delivered. Without this index the
        # same execution would be counted twice and the position size would be
        # wrong. NULL exchange_trade_id (paper fills) is exempt.
        Index(
            "uq_order_fill_exchange_trade_id",
            "order_id",
            "exchange_trade_id",
            unique=True,
            postgresql_where=text("exchange_trade_id IS NOT NULL"),
        ),
        Index("ix_order_fill_filled_at", "filled_at"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("price > 0", name="price_positive"),
        CheckConstraint("fee_amount >= 0", name="fee_non_negative"),
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("order.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: The exchange's identifier for this execution. The deduplication key.
    exchange_trade_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    quantity: Mapped[Decimal] = mapped_column(quantity(), nullable=False)
    price: Mapped[Decimal] = mapped_column(price(), nullable=False)
    fee_amount: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    fee_currency: Mapped[str | None] = mapped_column(currency_code(), nullable=True)
    is_maker: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    filled_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    order: Mapped[Order] = relationship(back_populates="fills")

    def __repr__(self) -> str:
        return f"<OrderFill {self.quantity}@{self.price}>"


class Position(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """An open exposure.

    There is deliberately **no** unique index limiting open positions to one.
    ``maximumOpenPositions`` is a configurable risk parameter (R-04); encoding
    today's value of 1 into the schema would turn a configuration change into a
    migration. The limit is enforced by the risk engine, which reads the current
    configuration, and ``PositionRepository.count_occupying_slots`` is what it
    counts with.
    """

    __tablename__ = "position"
    __table_args__ = (
        Index("ix_position_status", "status"),
        Index("ix_position_venue_status", "venue", "status"),
        CheckConstraint("quantity >= 0", name="quantity_non_negative"),
        CheckConstraint("entry_price > 0", name="entry_price_positive"),
        CheckConstraint(
            "stop_loss_price IS NULL OR stop_loss_price > 0", name="stop_loss_price_positive"
        ),
    )

    trading_pair_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_pair.id"), nullable=False
    )
    venue: Mapped[ExecutionVenue] = mapped_column(
        enum_column(ExecutionVenue, name="execution_venue"), nullable=False
    )

    #: Attribution rule (OD-04): the day the position was OPENED. A position may
    #: cross midnight, so the closing day is a different column.
    opened_trading_day_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_day.id"), nullable=False, index=True
    )
    closed_trading_day_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_day.id"), nullable=True
    )
    trading_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_session.id"), nullable=True
    )

    status: Mapped[PositionStatus] = mapped_column(
        enum_column(PositionStatus, name="position_status"),
        nullable=False,
        default=PositionStatus.OPENING,
    )
    side: Mapped[OrderSide] = mapped_column(
        enum_column(OrderSide, name="order_side"), nullable=False
    )

    quantity: Mapped[Decimal] = mapped_column(quantity(), nullable=False, default=ZERO)
    entry_price: Mapped[Decimal] = mapped_column(price(), nullable=False)
    stop_loss_price: Mapped[Decimal | None] = mapped_column(price(), nullable=True)
    take_profit_price: Mapped[Decimal | None] = mapped_column(price(), nullable=True)

    #: The 1R this position was sized against. Needed to compute the R multiple.
    risk_amount_quote: Mapped[Decimal | None] = mapped_column(monetary(), nullable=True)
    unrealised_pnl_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)

    opened_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    #: What the local state and the exchange disagreed about. Populated when the
    #: position enters DESYNCED, which blocks every new entry until resolved.
    desync_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    def __repr__(self) -> str:
        return f"<Position {self.side} {self.quantity} {self.status}>"


class Trade(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """A completed round trip. Append-only: the ledger is never rewritten."""

    __tablename__ = "trade"
    __table_args__ = (
        Index("ix_trade_closed_trading_day_id", "closed_trading_day_id"),
        Index("ix_trade_opened_trading_day_id", "opened_trading_day_id"),
        Index("ix_trade_closed_at", "closed_at"),
        CheckConstraint("quantity > 0", name="quantity_positive"),
        CheckConstraint("entry_price > 0", name="entry_price_positive"),
        CheckConstraint("exit_price > 0", name="exit_price_positive"),
        CheckConstraint("fees_quote >= 0", name="fees_non_negative"),
        CheckConstraint("closed_at >= opened_at", name="closed_after_opened"),
        # The accounting identity. If these three ever disagree, every report
        # built on the ledger is wrong, so the database refuses the row.
        CheckConstraint(
            "net_pnl_quote = gross_pnl_quote - fees_quote - slippage_quote",
            name="net_equals_gross_minus_costs",
        ),
        # A win is defined by the NET result, after costs. Storing a flag that
        # can disagree with the number it summarises is how reports start lying.
        CheckConstraint("is_win = (net_pnl_quote > 0)", name="is_win_matches_net"),
    )

    #: One trade per closed position.
    position_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("position.id"), nullable=False, unique=True
    )
    trading_pair_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_pair.id"), nullable=False
    )
    venue: Mapped[ExecutionVenue] = mapped_column(
        enum_column(ExecutionVenue, name="execution_venue"), nullable=False
    )

    #: Attribution rules from OD-04, recorded explicitly rather than inferred:
    #: the trade COUNT belongs to the opening day (R-05 limits new entries),
    #: while the realised P&L belongs to the closing day.
    opened_trading_day_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_day.id"), nullable=False
    )
    closed_trading_day_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_day.id"), nullable=False
    )
    trading_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_session.id"), nullable=True
    )

    quantity: Mapped[Decimal] = mapped_column(quantity(), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(price(), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(price(), nullable=False)

    gross_pnl_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False)
    fees_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    #: Signed cost: positive means execution was worse than the reference price.
    slippage_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    net_pnl_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False)

    #: The 1R the position was sized against, and the result expressed in R.
    risk_amount_quote: Mapped[Decimal | None] = mapped_column(monetary(), nullable=True)
    r_multiple: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    is_win: Mapped[bool] = mapped_column(Boolean, nullable=False)
    exit_reason: Mapped[ExitReason] = mapped_column(
        enum_column(ExitReason, name="exit_reason"), nullable=False
    )

    opened_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    closed_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    def __repr__(self) -> str:
        return f"<Trade net={self.net_pnl_quote} {self.exit_reason}>"
