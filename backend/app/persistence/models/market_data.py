"""Stored market data.

**Only closed candles are stored.** An in-progress candle repaints until it
closes; persisting one would let a backtest see values that were not visible at
decision time. The ingestion path filters on close time before anything reaches
this table.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, ForeignKey, Integer
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import CandleSource, Timeframe
from app.persistence.base import Base
from app.persistence.mixins import utc_now
from app.persistence.types import UtcDateTime, enum_column, monetary, price, quantity


class Candle(Base):
    """One closed candle.

    The primary key is the natural one: ``(trading_pair_id, timeframe,
    open_time)``. A candle has no identity apart from its coordinates - two rows
    with the same three values are the same candle, not two of them. A surrogate
    UUID would add sixteen bytes and a second index to a table that grows by
    ~35,000 rows per pair per year at 15 minutes, and would let a duplicate in.

    That key is also what makes ingestion idempotent: re-fetching a range after
    a restart inserts nothing new.
    """

    __tablename__ = "candle"
    __table_args__ = (
        # No extra index for "the newest candle for this pair", which is the
        # hottest query here. The primary key is already a btree on
        # (trading_pair_id, timeframe, open_time), and PostgreSQL scans a btree
        # backwards just as fast, so ORDER BY open_time DESC LIMIT 1 uses it.
        # A second index would cost write throughput and buy nothing.
        CheckConstraint("close_time > open_time", name="close_after_open"),
        CheckConstraint("open_price > 0", name="open_positive"),
        CheckConstraint("high_price > 0", name="high_positive"),
        CheckConstraint("low_price > 0", name="low_positive"),
        CheckConstraint("close_price > 0", name="close_positive"),
        CheckConstraint("high_price >= low_price", name="high_at_least_low"),
        # A candle whose open or close sits outside its own range cannot be
        # true. Storing one would poison every indicator computed over it.
        CheckConstraint(
            "open_price >= low_price AND open_price <= high_price", name="open_within_range"
        ),
        CheckConstraint(
            "close_price >= low_price AND close_price <= high_price", name="close_within_range"
        ),
        CheckConstraint("volume >= 0", name="volume_non_negative"),
        CheckConstraint("quote_volume >= 0", name="quote_volume_non_negative"),
        CheckConstraint("trade_count >= 0", name="trade_count_non_negative"),
    )

    trading_pair_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_pair.id", ondelete="CASCADE"), primary_key=True
    )
    timeframe: Mapped[Timeframe] = mapped_column(
        enum_column(Timeframe, name="timeframe"), primary_key=True
    )
    open_time: Mapped[datetime] = mapped_column(UtcDateTime, primary_key=True)

    close_time: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    open_price: Mapped[Decimal] = mapped_column(price(), nullable=False)
    high_price: Mapped[Decimal] = mapped_column(price(), nullable=False)
    low_price: Mapped[Decimal] = mapped_column(price(), nullable=False)
    close_price: Mapped[Decimal] = mapped_column(price(), nullable=False)

    #: Base asset traded, for example BTC.
    volume: Mapped[Decimal] = mapped_column(quantity(), nullable=False)
    #: Quote asset traded, for example USDT. Used for liquidity checks (R-12).
    quote_volume: Mapped[Decimal] = mapped_column(monetary(), nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False)

    #: Where this row came from. A candle backfilled after an outage was fetched
    #: under different conditions from one that arrived live.
    source: Mapped[CandleSource] = mapped_column(
        enum_column(CandleSource, name="candle_source"), nullable=False
    )
    ingested_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False, default=utc_now)

    def __repr__(self) -> str:
        return f"<Candle {self.timeframe} {self.open_time.isoformat()} c={self.close_price}>"
