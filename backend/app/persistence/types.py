"""Column types for the persistence layer.

Two guarantees are established here:

* **Money is NUMERIC with an explicit precision and scale.** Never ``FLOAT``,
  never an implicit default. The scale is chosen per kind of value, because a
  BTC quantity and a RON amount do not need the same number of decimals.
* **Every persisted timestamp is timezone-aware UTC.** A naive datetime is
  rejected on write rather than stored and misinterpreted later.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import DateTime, Dialect, Enum, Numeric, String, TypeDecorator

# ---------------------------------------------------------------------------
# Numeric precision
# ---------------------------------------------------------------------------
#
# PostgreSQL NUMERIC(precision, scale) is exact decimal arithmetic. The scale
# is the number of digits after the point and is chosen deliberately:
#
#   money      8 decimals is far beyond RON or USDT cent granularity, and
#              leaves room for fee and slippage sub-cents before rounding.
#   quantity  12 decimals covers assets with very small units; BTC needs 8.
#   price     12 decimals covers low-priced assets quoted with many decimals.
#   percent    4 decimals expresses 0.50% and 0.0125% exactly.
#   fx rate   12 decimals, so a RON conversion never loses a sub-cent.

MONETARY_PRECISION: Final = 24
MONETARY_SCALE: Final = 8

QUANTITY_PRECISION: Final = 28
QUANTITY_SCALE: Final = 12

PRICE_PRECISION: Final = 28
PRICE_SCALE: Final = 12

PERCENT_PRECISION: Final = 9
PERCENT_SCALE: Final = 4

FX_RATE_PRECISION: Final = 24
FX_RATE_SCALE: Final = 12


def monetary() -> Numeric[Any]:
    """An amount of money in some currency. The currency lives in its own column."""
    return Numeric(MONETARY_PRECISION, MONETARY_SCALE, asdecimal=True)


def quantity() -> Numeric[Any]:
    """A quantity of a base asset, for example 0.00042 BTC."""
    return Numeric(QUANTITY_PRECISION, QUANTITY_SCALE, asdecimal=True)


def price() -> Numeric[Any]:
    """A price expressed in the quote currency."""
    return Numeric(PRICE_PRECISION, PRICE_SCALE, asdecimal=True)


def percent() -> Numeric[Any]:
    """A percentage, stored as written: 0.50 means half a per cent."""
    return Numeric(PERCENT_PRECISION, PERCENT_SCALE, asdecimal=True)


def fx_rate() -> Numeric[Any]:
    """A conversion rate between two currencies."""
    return Numeric(FX_RATE_PRECISION, FX_RATE_SCALE, asdecimal=True)


# ---------------------------------------------------------------------------
# Currency codes
# ---------------------------------------------------------------------------

CURRENCY_CODE_LENGTH: Final = 10


def currency_code() -> String:
    """A currency or asset code such as RON, USDT or BTC."""
    return String(CURRENCY_CODE_LENGTH)


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware UTC.

    A naive datetime is rejected on write. Silently assuming a naive value is
    UTC is how a trading day boundary ends up shifted by three hours, which
    would corrupt daily P&L attribution in a way that is very hard to notice.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(
                "Naive datetime rejected. Persisted timestamps must be timezone-aware; "
                "use datetime.now(UTC)."
            )
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            # PostgreSQL always returns an offset for timestamptz; this guards
            # against a backend that does not.
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


def enum_column[E: StrEnum](enum_type: type[E], *, name: str) -> Enum:
    """Store a Python enum as VARCHAR with a CHECK constraint.

    Deliberately not a native PostgreSQL ENUM type. Adding a value to a native
    enum requires ``ALTER TYPE``, which does not run inside every transaction
    and complicates rollbacks. A VARCHAR with a CHECK constraint is dropped and
    recreated by an ordinary migration.
    """
    return Enum(
        enum_type,
        name=name,
        native_enum=False,
        create_constraint=True,
        validate_strings=True,
        length=max(len(member.value) for member in enum_type),
        values_callable=lambda enum_class: [member.value for member in enum_class],
    )
