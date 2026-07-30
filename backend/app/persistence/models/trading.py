"""Trading day and trading session.

**Currency convention.** Money on these rows is stored in the *exchange quote
currency* (USDT), because that is the currency the amounts were actually
realised in. RON figures are derived for reporting using
``funding_rate_ron_per_quote``, which is snapshotted onto the day.

Storing the rate on the day rather than looking it up later is deliberate: a
``TradingDay`` must remain fully explainable years afterwards, with the exact
capital, limits and rate it operated under, even if the configuration has since
changed ten times. That redundancy is the point of a snapshot.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import TradingDayStatus, TradingOutcome, TradingSessionStatus
from app.persistence.base import Base
from app.persistence.mixins import TimestampMixin, UuidPrimaryKeyMixin
from app.persistence.types import (
    UtcDateTime,
    currency_code,
    enum_column,
    fx_rate,
    monetary,
)

ZERO = Decimal(0)


class TradingDay(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """One calendar day in the trading timezone. See docs/SRS.md section 6."""

    __tablename__ = "trading_day"
    __table_args__ = (
        Index("ix_trading_day_status", "status"),
        CheckConstraint("trade_count >= 0", name="trade_count_non_negative"),
        CheckConstraint("session_count >= 0", name="session_count_non_negative"),
        CheckConstraint("consecutive_losses >= 0", name="consecutive_losses_non_negative"),
        CheckConstraint("reference_capital_ron > 0", name="reference_capital_positive"),
        CheckConstraint("funding_rate_ron_per_quote > 0", name="funding_rate_positive"),
    )

    #: One row per calendar date. The unique constraint is what makes "create
    #: the day if it does not exist" safe under concurrent schedulers.
    trading_date: Mapped[date] = mapped_column(nullable=False, unique=True)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)

    status: Mapped[TradingDayStatus] = mapped_column(
        enum_column(TradingDayStatus, name="trading_day_status"),
        nullable=False,
        default=TradingDayStatus.PENDING,
    )
    #: Reportable result. NO_TRADE is a normal value here, not an error.
    outcome: Mapped[TradingOutcome | None] = mapped_column(
        enum_column(TradingOutcome, name="trading_outcome"),
        nullable=True,
    )

    # ------------------------------------------------ governing configuration ---
    #: Which configuration versions governed this day. Without these, a risk
    #: decision made today cannot be explained after the next configuration
    #: change.
    risk_configuration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_configuration.id"), nullable=False
    )
    trading_configuration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_configuration.id"), nullable=False
    )

    # -------------------------------------------------- capital and currency ---
    reporting_currency: Mapped[str] = mapped_column(currency_code(), nullable=False)
    quote_currency: Mapped[str] = mapped_column(currency_code(), nullable=False)
    #: R-01, snapshotted. Fixed capital: this never changes with profit or loss.
    reference_capital_ron: Mapped[Decimal] = mapped_column(monetary(), nullable=False)
    #: Phase 0 decision OD-02: the rate locked at funding time, in RON per unit
    #: of quote currency. Fixed for the life of the funding, so RON limits map
    #: to quote amounts deterministically rather than moving with the market.
    funding_rate_ron_per_quote: Mapped[Decimal] = mapped_column(fx_rate(), nullable=False)
    #: The published rate used for RON reporting of this day, when one applies.
    fx_rate_snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("fx_rate_snapshot.id"), nullable=True
    )

    # ------------------------------------------------------------- balances ---
    opening_equity_quote: Mapped[Decimal | None] = mapped_column(monetary(), nullable=True)
    closing_equity_quote: Mapped[Decimal | None] = mapped_column(monetary(), nullable=True)

    # ------------------------------------------------------------------ pnl ---
    realised_pnl_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    unrealised_pnl_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    gross_profit_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    gross_loss_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    fees_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    slippage_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)

    # ----------------------------------------------------------- risk state ---
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    session_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    consecutive_losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: Highest realised P&L reached today. Recorded even though the daily profit
    #: floor is disabled (OD-03), so enabling R-23 later needs no backfill.
    peak_realised_pnl_quote: Mapped[Decimal] = mapped_column(
        monetary(), nullable=False, default=ZERO
    )

    #: Machine-readable reason the day stopped, from RiskReasonCode.
    stop_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stop_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    opened_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    sessions: Mapped[list[TradingSession]] = relationship(
        back_populates="trading_day",
        cascade="all, delete-orphan",
        order_by="TradingSession.sequence",
    )

    @property
    def net_pnl_quote(self) -> Decimal:
        """Realised plus unrealised: the basis rule R-26 evaluates (OD-06).

        Fees and slippage are already reflected in the realised figure; adding
        them here would double-count.
        """
        return self.realised_pnl_quote + self.unrealised_pnl_quote

    def __repr__(self) -> str:
        return f"<TradingDay {self.trading_date} {self.status}>"


class TradingSession(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A bounded sequence of trades inside one day.

    A day may contain zero, one or several sessions. A second session exists
    only when the first closed as CLOSED_RESTART_ELIGIBLE **and** a new
    opportunity independently satisfied every criterion.
    """

    __tablename__ = "trading_session"
    __table_args__ = (
        Index("ix_trading_session_status", "status"),
        Index("uq_trading_session_sequence", "trading_day_id", "sequence", unique=True),
        CheckConstraint("sequence >= 1", name="sequence_min"),
        CheckConstraint("trade_count >= 0", name="trade_count_non_negative"),
    )

    trading_day_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_day.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Position within the day: 1, 2, 3... Unique per day.
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)

    status: Mapped[TradingSessionStatus] = mapped_column(
        enum_column(TradingSessionStatus, name="trading_session_status"),
        nullable=False,
        default=TradingSessionStatus.EVALUATING,
    )

    started_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    realised_pnl_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    fees_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: Machine-readable reason the session closed, from RiskReasonCode.
    close_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_detail: Mapped[str | None] = mapped_column(Text, nullable=True)

    trading_day: Mapped[TradingDay] = relationship(back_populates="sessions")

    def __repr__(self) -> str:
        return f"<TradingSession #{self.sequence} {self.status}>"
