"""Symbol filter tests. Pure domain, no exchange involved."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.domain.symbol_filters import (
    LotSizeFilter,
    NotionalFilter,
    PriceFilter,
    SymbolFilterError,
    SymbolFilters,
    parse_symbol_filters,
)

PRICE_FILTER: dict[str, Any] = {
    "filterType": "PRICE_FILTER",
    "minPrice": "0.01000000",
    "maxPrice": "1000000.00000000",
    "tickSize": "0.01000000",
}
LOT_SIZE: dict[str, Any] = {
    "filterType": "LOT_SIZE",
    "minQty": "0.00001000",
    "maxQty": "9000.00000000",
    "stepSize": "0.00001000",
}
NOTIONAL: dict[str, Any] = {
    "filterType": "NOTIONAL",
    "minNotional": "5.00000000",
    "maxNotional": "9000000.00000000",
    "applyMinToMarket": True,
}
LEGACY_MIN_NOTIONAL: dict[str, Any] = {
    "filterType": "MIN_NOTIONAL",
    "minNotional": "10.00000000",
    "applyToMarket": True,
    "avgPriceMins": 5,
}


class TestParsing:
    def test_the_three_filters_we_depend_on_are_parsed(self) -> None:
        filters = parse_symbol_filters("BTCUSDT", [PRICE_FILTER, LOT_SIZE, NOTIONAL])
        assert filters.is_complete_for_trading is True
        assert filters.price is not None
        assert filters.lot_size is not None
        assert filters.notional is not None

    def test_an_unknown_filter_type_is_ignored(self) -> None:
        """The exchange adds filters. Refusing to start over one we do not use
        would be a denial of service we inflicted on ourselves."""
        filters = parse_symbol_filters(
            "BTCUSDT",
            [PRICE_FILTER, LOT_SIZE, NOTIONAL, {"filterType": "SOMETHING_NEW", "x": "1"}],
        )
        assert filters.is_complete_for_trading is True

    def test_a_missing_filter_leaves_the_set_incomplete(self) -> None:
        """Absent is "not read", never "no limit"."""
        filters = parse_symbol_filters("BTCUSDT", [PRICE_FILTER])
        assert filters.is_complete_for_trading is False
        assert filters.lot_size is None

    def test_a_float_in_a_filter_field_is_refused(self) -> None:
        """The exchange sends strings. A float means precision was already lost."""
        broken = dict(PRICE_FILTER, tickSize=0.01)
        with pytest.raises(SymbolFilterError, match="float"):
            parse_symbol_filters("BTCUSDT", [broken])

    def test_a_non_numeric_filter_field_is_refused(self) -> None:
        broken = dict(PRICE_FILTER, tickSize="not a number")
        with pytest.raises(SymbolFilterError, match="not a number"):
            parse_symbol_filters("BTCUSDT", [broken])


class TestTwoNotionalFilters:
    """MIN_NOTIONAL and NOTIONAL both exist and are different filters."""

    def test_the_legacy_filter_is_used_when_it_is_the_only_one(self) -> None:
        filters = parse_symbol_filters("BTCUSDT", [LEGACY_MIN_NOTIONAL])
        assert filters.notional is not None
        assert filters.notional.min_notional == Decimal("10.00000000")
        # The legacy filter publishes no upper bound.
        assert filters.notional.max_notional == Decimal(0)

    def test_the_richer_filter_wins_when_both_are_present(self) -> None:
        """NOTIONAL carries an upper bound; MIN_NOTIONAL must not overwrite it."""
        filters = parse_symbol_filters("BTCUSDT", [NOTIONAL, LEGACY_MIN_NOTIONAL])
        assert filters.notional is not None
        assert filters.notional.min_notional == Decimal("5.00000000")
        assert filters.notional.max_notional == Decimal("9000000.00000000")

    def test_order_in_the_list_does_not_change_the_outcome(self) -> None:
        first = parse_symbol_filters("BTCUSDT", [LEGACY_MIN_NOTIONAL, NOTIONAL])
        second = parse_symbol_filters("BTCUSDT", [NOTIONAL, LEGACY_MIN_NOTIONAL])
        assert first.notional == second.notional


class TestPriceFilter:
    def test_a_price_off_the_tick_grid_is_rejected(self) -> None:
        price_filter = PriceFilter(
            min_price=Decimal("0.01"), max_price=Decimal("1000000"), tick_size=Decimal("0.01")
        )
        assert price_filter.is_satisfied_by(Decimal("65000.01")) is True
        assert price_filter.is_satisfied_by(Decimal("65000.015")) is False

    def test_rounding_snaps_downwards_onto_the_grid(self) -> None:
        price_filter = PriceFilter(
            min_price=Decimal("0.01"), max_price=Decimal("1000000"), tick_size=Decimal("0.01")
        )
        assert price_filter.round_price(Decimal("65000.019")) == Decimal("65000.01")

    def test_a_zero_bound_means_no_bound(self) -> None:
        price_filter = PriceFilter(
            min_price=Decimal(0), max_price=Decimal(0), tick_size=Decimal("0.01")
        )
        assert price_filter.is_satisfied_by(Decimal("999999999.99")) is True


class TestLotSizeFilter:
    def test_quantities_round_down_never_up(self) -> None:
        """Rounding up would spend more than the approved risk budget."""
        lot = LotSizeFilter(
            min_quantity=Decimal("0.00001"),
            max_quantity=Decimal("9000"),
            step_size=Decimal("0.00001"),
        )
        raw = Decimal("0.001679999")
        rounded = lot.round_quantity(raw)
        assert rounded == Decimal("0.00167")
        assert rounded < raw

    def test_a_quantity_below_the_minimum_is_rejected(self) -> None:
        lot = LotSizeFilter(
            min_quantity=Decimal("0.001"),
            max_quantity=Decimal("9000"),
            step_size=Decimal("0.001"),
        )
        assert lot.is_satisfied_by(Decimal("0.0005")) is False

    def test_the_rounded_quantity_always_satisfies_the_filter(self) -> None:
        lot = LotSizeFilter(
            min_quantity=Decimal("0.00001"),
            max_quantity=Decimal("9000"),
            step_size=Decimal("0.00001"),
        )
        for raw in ("0.001679999", "1.23456789", "0.000019999"):
            rounded = lot.round_quantity(Decimal(raw))
            assert lot.is_satisfied_by(rounded)


class TestNotionalFilter:
    def test_an_order_below_the_minimum_notional_is_rejected(self) -> None:
        notional = NotionalFilter(
            min_notional=Decimal("5"),
            max_notional=Decimal("9000000"),
            applies_to_market_orders=True,
        )
        assert notional.is_satisfied_by(Decimal("4.99")) is False
        assert notional.is_satisfied_by(Decimal("5.00")) is True

    def test_a_zero_maximum_means_no_upper_bound(self) -> None:
        notional = NotionalFilter(
            min_notional=Decimal("5"), max_notional=Decimal(0), applies_to_market_orders=False
        )
        assert notional.is_satisfied_by(Decimal("99999999")) is True


class TestSizingAgainstRealFilters:
    """The end-to-end arithmetic the risk engine will perform in Phase 5."""

    def test_rounding_reduces_risk_and_the_result_stays_on_the_grid(self) -> None:
        filters = parse_symbol_filters("BTCUSDT", [PRICE_FILTER, LOT_SIZE, NOTIONAL])
        assert filters.lot_size is not None and filters.notional is not None

        risk_quote = Decimal("1.0870")  # 5.00 RON at 4.60 RON per USDT
        entry = Decimal("65000.00")
        stop = Decimal("64350.00")
        stop_distance = entry - stop

        raw_quantity = risk_quote / stop_distance
        quantity = filters.lot_size.round_quantity(raw_quantity)

        assert filters.lot_size.is_satisfied_by(quantity)
        # Rounding down can only reduce the real risk, never increase it.
        assert quantity * stop_distance <= risk_quote

    def test_a_position_too_small_for_the_minimum_notional_is_detectable(self) -> None:
        """The quantity is never inflated to reach the minimum: that would
        silently exceed the risk budget. The order is refused instead."""
        filters = parse_symbol_filters("BTCUSDT", [PRICE_FILTER, LOT_SIZE, NOTIONAL])
        assert filters.notional is not None

        quantity = Decimal("0.00001")
        entry = Decimal("65000.00")
        notional = quantity * entry  # 0.65 USDT, below the 5 USDT minimum

        assert filters.notional.is_satisfied_by(notional) is False


class TestCompleteness:
    def test_an_empty_filter_set_is_incomplete(self) -> None:
        assert SymbolFilters(symbol="BTCUSDT").is_complete_for_trading is False
