"""Balance and P&L snapshots.

Both are append-only observations, never running totals that get updated. A
snapshot answers "what was true at this instant", and rewriting one would make
the reconciliation history meaningless.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import ExecutionVenue
from app.persistence.base import Base
from app.persistence.mixins import CreatedAtMixin, UuidPrimaryKeyMixin
from app.persistence.types import UtcDateTime, currency_code, enum_column, fx_rate, monetary

ZERO = Decimal(0)


class BalanceSnapshot(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """One asset balance as observed at one instant."""

    __tablename__ = "balance_snapshot"
    __table_args__ = (
        Index("ix_balance_snapshot_taken_at", "taken_at"),
        Index("ix_balance_snapshot_venue_asset", "venue", "asset", "taken_at"),
        CheckConstraint("free_amount >= 0", name="free_non_negative"),
        CheckConstraint("locked_amount >= 0", name="locked_non_negative"),
        # A total that does not equal free plus locked is a parsing bug, and a
        # wrong balance is how an order gets sized against money that is not
        # there.
        CheckConstraint(
            "total_amount = free_amount + locked_amount", name="total_equals_free_plus_locked"
        ),
    )

    taken_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    venue: Mapped[ExecutionVenue] = mapped_column(
        enum_column(ExecutionVenue, name="execution_venue"), nullable=False
    )
    #: Where the numbers came from: EXCHANGE, PAPER or RECONCILIATION.
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    asset: Mapped[str] = mapped_column(currency_code(), nullable=False)
    free_amount: Mapped[Decimal] = mapped_column(monetary(), nullable=False)
    locked_amount: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    total_amount: Mapped[Decimal] = mapped_column(monetary(), nullable=False)

    trading_day_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_day.id"), nullable=True, index=True
    )

    def __repr__(self) -> str:
        return f"<BalanceSnapshot {self.asset} {self.total_amount}>"


class PnLSnapshot(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """The day's profit and loss as observed at one instant."""

    __tablename__ = "pnl_snapshot"
    __table_args__ = (
        Index("ix_pnl_snapshot_day_taken_at", "trading_day_id", "taken_at"),
        CheckConstraint("open_position_count >= 0", name="open_position_count_non_negative"),
        CheckConstraint("funding_rate_ron_per_quote > 0", name="funding_rate_positive"),
        # Rule R-26, Phase 0 decision OD-06: the daily limit is evaluated on
        # realised plus unrealised. The identity is enforced so a snapshot can
        # never disagree with the basis the risk engine uses.
        CheckConstraint(
            "net_pnl_quote = realised_pnl_quote + unrealised_pnl_quote",
            name="net_equals_realised_plus_unrealised",
        ),
    )

    trading_day_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_day.id", ondelete="CASCADE"), nullable=False
    )
    taken_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    realised_pnl_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    unrealised_pnl_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    net_pnl_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    fees_quote: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)
    equity_quote: Mapped[Decimal | None] = mapped_column(monetary(), nullable=True)

    open_position_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    #: The rate this snapshot's RON figure was computed with, stored so the
    #: reported number stays reproducible even if the rate is ever changed.
    funding_rate_ron_per_quote: Mapped[Decimal] = mapped_column(fx_rate(), nullable=False)
    net_pnl_ron: Mapped[Decimal] = mapped_column(monetary(), nullable=False, default=ZERO)

    def __repr__(self) -> str:
        return f"<PnLSnapshot net={self.net_pnl_quote}>"
