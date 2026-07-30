"""Monetary values and exchange-filter rounding.

Two rules are enforced here and nowhere else:

1. **No binary floating point.** ``float`` cannot represent 0.10 exactly.
   Accumulated error in a ledger is a correctness bug, not a display detail.
   ``to_decimal`` rejects ``float`` at runtime, not merely in type hints:
   mypy checks what is declared, not what arrives from JSON or a third-party
   library.

2. **No silent currency mixing.** ``Money`` carries its currency. Adding RON to
   USDT raises instead of producing a meaningless number. Conversion must be
   explicit and must go through a recorded FX rate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal, localcontext
from typing import Final

from app.domain.errors import CurrencyMismatchError, MoneyError

#: Working precision for intermediate arithmetic. Well above anything crypto
#: quantities or RON amounts need, so a division never silently loses digits.
CALCULATION_PRECISION: Final = 60

#: Two decimal places, the granularity of RON reporting.
CENT: Final = Decimal("0.01")

#: Decimal places a price is stored with. MUST match the scale of ``price()`` in
#: app/persistence/types.py, and a test asserts that it does.
#:
#: It exists in the domain because a value that cannot be stored as computed is
#: a value the audit trail will disagree with: whatever a decision emits beyond
#: this is silently dropped on the way to the database, and the record would no
#: longer be the decision.
PRICE_PLACES: Final = 12
PRICE_QUANTUM: Final = Decimal(1).scaleb(-PRICE_PLACES)


def quantize_price(value: Decimal) -> Decimal:
    """Round a price to the precision the system can actually persist.

    ``ROUND_HALF_UP``: this is a presentation-level rounding of a value that is
    already far more precise than any exchange tick, so no directional bias is
    introduced. Rounding that must be directional - a quantity down to the lot
    size, a stop away from the entry - happens later, against the exchange's
    own filters, and is not this.
    """
    return value.quantize(PRICE_QUANTUM, rounding=ROUND_HALF_UP)


_CURRENCY_CODE_PATTERN: Final = re.compile(r"^[A-Z0-9]{2,10}$")

DecimalLike = Decimal | int | str


def to_decimal(value: DecimalLike) -> Decimal:
    """Convert to ``Decimal``, refusing ``float``.

    ``bool`` is rejected as well: it is a subclass of ``int``, and a quantity of
    ``True`` is always a bug rather than an intention.
    """
    if isinstance(value, bool):
        raise MoneyError(f"bool is not a monetary value: {value!r}")
    if isinstance(value, float):
        raise MoneyError(
            f"float is forbidden for monetary values: {value!r}. "
            f"Pass a string or a Decimal, for example Decimal('{value!r}')."
        )
    if isinstance(value, Decimal):
        result = value
    else:
        try:
            result = Decimal(value)
        except Exception as exc:
            raise MoneyError(f"Cannot convert to Decimal: {value!r}") from exc

    if not result.is_finite():
        raise MoneyError(f"Non-finite monetary value: {value!r}")
    return result


@dataclass(frozen=True, slots=True, order=True)
class Currency:
    """An ISO-like currency or asset code, for example RON, USDT, BTC."""

    code: str

    def __post_init__(self) -> None:
        if not _CURRENCY_CODE_PATTERN.match(self.code):
            raise MoneyError(
                f"Invalid currency code: {self.code!r}. Expected 2-10 uppercase letters or digits."
            )

    def __str__(self) -> str:
        return self.code


RON: Final = Currency("RON")
USD: Final = Currency("USD")
EUR: Final = Currency("EUR")
USDT: Final = Currency("USDT")
BTC: Final = Currency("BTC")
ETH: Final = Currency("ETH")


@dataclass(frozen=True, slots=True)
class Money:
    """An exact amount in a specific currency."""

    amount: Decimal
    currency: Currency

    def __post_init__(self) -> None:
        # Bypasses the frozen guard on purpose, to normalise on construction.
        object.__setattr__(self, "amount", to_decimal(self.amount))

    @classmethod
    def of(cls, amount: DecimalLike, currency: Currency) -> Money:
        return cls(to_decimal(amount), currency)

    @classmethod
    def zero(cls, currency: Currency) -> Money:
        return cls(Decimal(0), currency)

    # ------------------------------------------------------------- guards ---

    def _require_same_currency(self, other: Money, operation: str) -> None:
        if self.currency != other.currency:
            raise CurrencyMismatchError(self.currency.code, other.currency.code, operation)

    # --------------------------------------------------------- arithmetic ---

    def __add__(self, other: Money) -> Money:
        self._require_same_currency(other, "add")
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: Money) -> Money:
        self._require_same_currency(other, "subtract")
        return Money(self.amount - other.amount, self.currency)

    def __neg__(self) -> Money:
        return Money(-self.amount, self.currency)

    def __abs__(self) -> Money:
        return Money(abs(self.amount), self.currency)

    def scaled_by(self, factor: DecimalLike) -> Money:
        """Multiply by a dimensionless factor, for example a quantity."""
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            return Money(self.amount * to_decimal(factor), self.currency)

    def ratio_to(self, other: Money) -> Decimal:
        """Divide by another amount in the same currency, giving a plain ratio."""
        self._require_same_currency(other, "divide")
        if other.amount == 0:
            raise MoneyError("Division by a zero amount")
        with localcontext() as context:
            context.prec = CALCULATION_PRECISION
            return self.amount / other.amount

    # -------------------------------------------------------- comparisons ---

    def __lt__(self, other: Money) -> bool:
        self._require_same_currency(other, "compare")
        return self.amount < other.amount

    def __le__(self, other: Money) -> bool:
        self._require_same_currency(other, "compare")
        return self.amount <= other.amount

    def __gt__(self, other: Money) -> bool:
        self._require_same_currency(other, "compare")
        return self.amount > other.amount

    def __ge__(self, other: Money) -> bool:
        self._require_same_currency(other, "compare")
        return self.amount >= other.amount

    # ------------------------------------------------------------ helpers ---

    @property
    def is_zero(self) -> bool:
        return self.amount == 0

    @property
    def is_positive(self) -> bool:
        return self.amount > 0

    @property
    def is_negative(self) -> bool:
        return self.amount < 0

    def quantized(self, exponent: Decimal = CENT, rounding: str = ROUND_HALF_UP) -> Money:
        """Round to a given exponent. Explicit rounding mode, never a default guess."""
        return Money(self.amount.quantize(exponent, rounding=rounding), self.currency)

    def __str__(self) -> str:
        return f"{self.amount} {self.currency.code}"


# ------------------------------------------------------------- percentages ---


def percent_of(base: Decimal, percent: DecimalLike, *, rounding: str = ROUND_FLOOR) -> Decimal:
    """Compute ``percent`` per cent of ``base``, rounded to the cent.

    Rounds DOWN by default. Every caller in this system computes a *limit*
    (risk per trade, maximum daily loss), and rounding a limit up - even by one
    cent - would permit more risk than configured.
    """
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        raw = to_decimal(base) * to_decimal(percent) / Decimal(100)
    return raw.quantize(CENT, rounding=rounding)


# ------------------------------------------------- exchange filter rounding ---


def _require_positive_step(step: Decimal, label: str) -> Decimal:
    step = to_decimal(step)
    if step <= 0:
        raise MoneyError(f"{label} must be positive, got {step}")
    return step


def round_down_to_step(value: DecimalLike, step: DecimalLike) -> Decimal:
    """Round down to the nearest multiple of ``step``.

    This is the direction used for order quantities. Rounding a quantity UP to
    satisfy an exchange filter would silently increase the position beyond the
    approved risk budget, which is why the opposite helper exists but is never
    used for quantities.
    """
    decimal_value = to_decimal(value)
    decimal_step = _require_positive_step(to_decimal(step), "step")
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        multiples = (decimal_value / decimal_step).to_integral_value(rounding=ROUND_FLOOR)
        return (multiples * decimal_step).quantize(decimal_step)


def round_up_to_step(value: DecimalLike, step: DecimalLike) -> Decimal:
    """Round up to the nearest multiple of ``step``.

    Used for protective levels, never for quantities.
    """
    decimal_value = to_decimal(value)
    decimal_step = _require_positive_step(to_decimal(step), "step")
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        multiples = (decimal_value / decimal_step).to_integral_value(rounding=ROUND_CEILING)
        return (multiples * decimal_step).quantize(decimal_step)


def is_multiple_of_step(value: DecimalLike, step: DecimalLike) -> bool:
    """Whether ``value`` sits exactly on the exchange's grid."""
    decimal_value = to_decimal(value)
    decimal_step = _require_positive_step(to_decimal(step), "step")
    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        return decimal_value % decimal_step == 0
