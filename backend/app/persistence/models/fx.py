"""Foreign exchange rate snapshots."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.persistence.base import Base
from app.persistence.mixins import CreatedAtMixin, UuidPrimaryKeyMixin
from app.persistence.types import UtcDateTime, currency_code, fx_rate


class FxRateSnapshot(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """One published exchange rate, stored so reports stay reproducible.

    Recomputing a past day's P&L with today's rate would produce a different
    number than the one originally reported. Binding a snapshot to each trading
    day makes historical reports stable forever.

    BNR publishes on working days only. On weekends and holidays the last
    published rate is reused, still carrying its original ``rate_date`` - the
    system is transparent about the gap rather than inventing a value.
    """

    __tablename__ = "fx_rate_snapshot"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "base_currency",
            "quote_currency",
            "rate_date",
            name="uq_fx_rate_snapshot_source_pair_date",
        ),
        CheckConstraint("rate > 0", name="rate_positive"),
        CheckConstraint("base_currency <> quote_currency", name="currencies_differ"),
    )

    #: Publisher of the rate, for example BNR. Part of the identity: two sources
    #: may legitimately disagree, and a report must say which one it used.
    source: Mapped[str] = mapped_column(String(32), nullable=False)

    base_currency: Mapped[str] = mapped_column(currency_code(), nullable=False)
    quote_currency: Mapped[str] = mapped_column(currency_code(), nullable=False)
    rate: Mapped[Decimal] = mapped_column(fx_rate(), nullable=False)

    #: The date the rate applies to, as published. For a weekend this stays the
    #: preceding Friday, which is what makes the reuse visible instead of silent.
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)

    #: When the application actually retrieved it.
    fetched_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)

    #: True when this rate was reused for a day the source did not publish.
    is_carried_forward: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return f"<FxRateSnapshot {self.base_currency}/{self.quote_currency}={self.rate}>"
