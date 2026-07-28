"""Money and rounding tests.

Covers AC-17 (money is Decimal, never float) and the requirement that RON is
never silently converted into the exchange quote currency.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.errors import CurrencyMismatchError, MoneyError
from app.domain.money import (
    BTC,
    CENT,
    RON,
    USDT,
    Currency,
    Money,
    is_multiple_of_step,
    percent_of,
    round_down_to_step,
    round_up_to_step,
    to_decimal,
)


class TestToDecimal:
    def test_accepts_strings_and_integers(self) -> None:
        assert to_decimal("1000.00") == Decimal("1000.00")
        assert to_decimal(1000) == Decimal(1000)
        assert to_decimal(Decimal("0.1")) == Decimal("0.1")

    def test_preserves_exact_string_precision(self) -> None:
        """0.1 is not representable in binary. As a string it is exact."""
        assert to_decimal("0.1") * 3 == Decimal("0.3")

    def test_rejects_float(self) -> None:
        with pytest.raises(MoneyError, match="float is forbidden"):
            to_decimal(0.1)  # type: ignore[arg-type]

    def test_rejects_bool(self) -> None:
        """bool is an int subclass, so the type checker accepts it silently.

        No ``type: ignore`` here on purpose: mypy sees nothing wrong with
        ``to_decimal(True)``, which is precisely why the runtime guard exists.
        """
        with pytest.raises(MoneyError, match="bool is not a monetary value"):
            to_decimal(True)

    def test_rejects_nan_and_infinity(self) -> None:
        for value in ("NaN", "Infinity", "-Infinity"):
            with pytest.raises(MoneyError, match="Non-finite"):
                to_decimal(value)

    def test_rejects_garbage(self) -> None:
        with pytest.raises(MoneyError, match="Cannot convert"):
            to_decimal("not a number")


class TestCurrency:
    def test_accepts_valid_codes(self) -> None:
        for code in ("RON", "USDT", "BTC", "EUR", "USD"):
            assert Currency(code).code == code

    def test_rejects_invalid_codes(self) -> None:
        for code in ("ron", "R", "TOOLONGCODE1", "US$", ""):
            with pytest.raises(MoneyError, match="Invalid currency code"):
                Currency(code)

    def test_is_comparable_and_hashable(self) -> None:
        assert Currency("RON") == RON
        assert len({Currency("RON"), RON, USDT}) == 2


class TestMoneyArithmetic:
    def test_adds_and_subtracts_same_currency(self) -> None:
        assert Money.of("10.50", RON) + Money.of("4.50", RON) == Money.of("15.00", RON)
        assert Money.of("10.50", RON) - Money.of("0.50", RON) == Money.of("10.00", RON)

    def test_scaling_by_a_quantity(self) -> None:
        price = Money.of("65000.00", USDT)
        assert price.scaled_by(Decimal("0.001")) == Money.of("65.000", USDT)

    def test_ratio_between_amounts(self) -> None:
        assert Money.of("20.00", RON).ratio_to(Money.of("1000.00", RON)) == Decimal("0.02")

    def test_rejects_division_by_zero(self) -> None:
        with pytest.raises(MoneyError, match="zero amount"):
            Money.of("1", RON).ratio_to(Money.zero(RON))

    def test_rejects_float_amounts(self) -> None:
        with pytest.raises(MoneyError, match="float is forbidden"):
            Money.of(1000.0, RON)  # type: ignore[arg-type]


class TestCurrencyIsolation:
    """The requirement: never silently convert RON into the quote currency."""

    def test_adding_different_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError, match="RON and USDT"):
            _ = Money.of("1000.00", RON) + Money.of("200.00", USDT)

    def test_subtracting_different_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            _ = Money.of("1000.00", RON) - Money.of("200.00", USDT)

    def test_comparing_different_currencies_raises(self) -> None:
        with pytest.raises(CurrencyMismatchError):
            _ = Money.of("1000.00", RON) < Money.of("200.00", USDT)

    def test_the_error_names_both_currencies_and_the_fix(self) -> None:
        with pytest.raises(CurrencyMismatchError) as caught:
            _ = Money.of("1", RON) + Money.of("1", BTC)
        message = str(caught.value)
        assert "RON" in message
        assert "BTC" in message
        assert "FX rate" in message

    def test_equal_amounts_in_different_currencies_are_not_equal(self) -> None:
        assert Money.of("1000.00", RON) != Money.of("1000.00", USDT)


class TestPercentOf:
    def test_phase_zero_risk_amounts(self) -> None:
        capital = Decimal("1000.00")
        assert percent_of(capital, "2.00") == Decimal("20.00")
        assert percent_of(capital, "4.00") == Decimal("40.00")
        assert percent_of(capital, "0.50") == Decimal("5.00")

    def test_rounds_down_never_up(self) -> None:
        """Rounding a risk limit up would permit more risk than configured."""
        assert percent_of(Decimal("1000.00"), "0.3339") == Decimal("3.33")
        assert percent_of(Decimal("1000.00"), "0.9999") == Decimal("9.99")

    def test_result_always_has_two_decimals(self) -> None:
        assert percent_of(Decimal("1000.00"), "1") == Decimal("10.00")
        assert percent_of(Decimal("1000.00"), "1").as_tuple().exponent == CENT.as_tuple().exponent


class TestExchangeFilterRounding:
    @pytest.mark.parametrize(
        ("value", "step", "expected"),
        [
            ("0.123456789", "0.00001", "0.12345"),
            ("0.99999999", "0.00001", "0.99999"),
            ("1.0", "0.00001", "1.00000"),
            ("123.456", "0.01", "123.45"),
            ("7", "1", "7"),
            ("0.000001", "0.00001", "0.00000"),
        ],
    )
    def test_round_down_to_step(self, value: str, step: str, expected: str) -> None:
        assert round_down_to_step(value, step) == Decimal(expected)

    def test_round_down_never_increases_the_value(self) -> None:
        for raw in ("0.123456789", "9.999999", "0.5", "1000.0001"):
            assert round_down_to_step(raw, "0.001") <= Decimal(raw)

    def test_round_up_to_step(self) -> None:
        assert round_up_to_step("0.123456789", "0.00001") == Decimal("0.12346")
        assert round_up_to_step("1.00000", "0.00001") == Decimal("1.00000")

    def test_result_sits_exactly_on_the_grid(self) -> None:
        for raw in ("0.123456789", "9.87654321", "0.5"):
            assert is_multiple_of_step(round_down_to_step(raw, "0.00001"), "0.00001")

    def test_rejects_non_positive_step(self) -> None:
        for step in ("0", "-0.001"):
            with pytest.raises(MoneyError, match="must be positive"):
                round_down_to_step("1.0", step)

    def test_rounding_changes_the_effective_risk(self) -> None:
        """The reason the risk engine must recompute risk after rounding.

        5.00 RON of risk over a 0.9 RON stop distance is 5.5555... units. The
        exchange grid forces 5.5, so the real risk becomes 4.95 RON - lower,
        which is safe. Rounding the other way would have made it 5.04 RON,
        which is not.
        """
        risk = Decimal("5.00")
        stop_distance = Decimal("0.90")
        raw_quantity = risk / stop_distance
        quantity = round_down_to_step(raw_quantity, "0.1")

        assert quantity == Decimal("5.5")
        assert quantity * stop_distance == Decimal("4.95")
        assert quantity * stop_distance <= risk
