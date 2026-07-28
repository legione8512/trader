"""Reference data: exchanges and trading pairs."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.base import Base
from app.persistence.mixins import TimestampMixin, UuidPrimaryKeyMixin
from app.persistence.types import UtcDateTime, currency_code


class Exchange(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A venue the application can connect to."""

    __tablename__ = "exchange"

    #: Stable machine code, for example BINANCE. Never displayed-name based.
    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    trading_pairs: Mapped[list[TradingPair]] = relationship(
        back_populates="exchange",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Exchange {self.code}>"


class TradingPair(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A tradable symbol on an exchange, for example BTCUSDT on Binance."""

    __tablename__ = "trading_pair"
    __table_args__ = (UniqueConstraint("exchange_id", "symbol", name="uq_trading_pair_symbol"),)

    exchange_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("exchange.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Exchange-native symbol, exactly as the exchange spells it.
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    base_asset: Mapped[str] = mapped_column(currency_code(), nullable=False)
    quote_asset: Mapped[str] = mapped_column(currency_code(), nullable=False)

    #: Whether the application is allowed to trade this pair at all.
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Trading status as reported by the exchange, for example TRADING or BREAK.
    #: Populated in Phase 3 from the official exchange information endpoint.
    exchange_status: Mapped[str | None] = mapped_column(String(32), nullable=True)

    #: Raw symbol filters (tick size, step size, minimum notional) exactly as
    #: the exchange returns them. Stored verbatim rather than parsed into
    #: columns: the authoritative shape is the exchange's, and inventing our own
    #: column names before reading the official documentation would be guessing.
    filters: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    filters_synced_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    exchange: Mapped[Exchange] = relationship(back_populates="trading_pairs")

    def __repr__(self) -> str:
        return f"<TradingPair {self.symbol}>"
