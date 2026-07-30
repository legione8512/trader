"""Exchange symbol filters, as domain values.

Pure module. The exchange's raw JSON is parsed once, at the edge, into these
value objects; from then on the risk engine works with typed Decimals instead of
digging through a list of dictionaries.

The rules implemented here are quoted from the official filter documentation:
  https://developers.binance.com/docs/binance-spot-api-docs/filters

Two details are easy to get wrong and are handled explicitly:

* **There are two notional filters.** ``MIN_NOTIONAL`` and ``NOTIONAL`` are
  different filters with different fields; a symbol may carry either. Handling
  only one is a silent way to size an order the exchange will reject.
* **Rounding direction is not symmetric.** Quantities round DOWN, so a rounding
  error can only ever reduce risk. Never up.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.domain.errors import DomainError
from app.domain.money import (
    is_multiple_of_step,
    round_down_to_step,
    round_up_to_step,
    to_decimal,
)


class SymbolFilterError(DomainError):
    """The exchange sent filters we cannot interpret."""


@dataclass(frozen=True, slots=True)
class PriceFilter:
    """PRICE_FILTER: price >= minPrice, <= maxPrice, and a multiple of tickSize."""

    min_price: Decimal
    max_price: Decimal
    tick_size: Decimal

    def is_satisfied_by(self, price: Decimal) -> bool:
        # A zero bound means "no bound", which is how the exchange disables one.
        if self.min_price > 0 and price < self.min_price:
            return False
        if self.max_price > 0 and price > self.max_price:
            return False
        return self.tick_size <= 0 or is_multiple_of_step(price, self.tick_size)

    def round_price_down(self, price: Decimal) -> Decimal:
        """Snap a price onto the exchange's grid, downwards."""
        if self.tick_size <= 0:
            return price
        return round_down_to_step(price, self.tick_size)

    def round_price_up(self, price: Decimal) -> Decimal:
        """Snap a price onto the exchange's grid, upwards."""
        if self.tick_size <= 0:
            return price
        return round_up_to_step(price, self.tick_size)

    # NOTE: there is deliberately no "round away from this anchor" helper.
    #
    # One existed and was removed. Choosing the direction by comparing the price
    # against the ALREADY-ROUNDED entry inverted a long position whenever the
    # entry rounded down past the stop: the stop then compared as "above" and
    # was rounded further up, landing on the wrong side of the entry entirely.
    # The direction belongs to the side of the trade, which the caller knows and
    # this filter does not.


@dataclass(frozen=True, slots=True)
class LotSizeFilter:
    """LOT_SIZE: quantity >= minQty, <= maxQty, and a multiple of stepSize."""

    min_quantity: Decimal
    max_quantity: Decimal
    step_size: Decimal

    def is_satisfied_by(self, quantity: Decimal) -> bool:
        if quantity < self.min_quantity:
            return False
        if self.max_quantity > 0 and quantity > self.max_quantity:
            return False
        return self.step_size <= 0 or is_multiple_of_step(quantity, self.step_size)

    def round_quantity(self, quantity: Decimal) -> Decimal:
        """Snap a quantity onto the grid, DOWNWARDS, always.

        Rounding up to satisfy a filter would spend more than the approved risk
        budget. The risk engine recomputes the real risk from this result and
        refuses the order if it still exceeds the limit.
        """
        if self.step_size <= 0:
            return quantity
        return round_down_to_step(quantity, self.step_size)


@dataclass(frozen=True, slots=True)
class NotionalFilter:
    """The notional bounds, from either ``NOTIONAL`` or ``MIN_NOTIONAL``.

    Both are normalised into this one shape. ``max_notional`` is zero when the
    exchange did not publish one, which is the case for ``MIN_NOTIONAL``.
    """

    min_notional: Decimal
    max_notional: Decimal
    applies_to_market_orders: bool

    def is_satisfied_by(self, notional: Decimal) -> bool:
        if self.min_notional > 0 and notional < self.min_notional:
            return False
        return self.max_notional <= 0 or notional <= self.max_notional


@dataclass(frozen=True, slots=True)
class SymbolFilters:
    """Everything the exchange says about how an order on this symbol may look."""

    symbol: str
    price: PriceFilter | None = None
    lot_size: LotSizeFilter | None = None
    notional: NotionalFilter | None = None

    @property
    def is_complete_for_trading(self) -> bool:
        """Whether enough is known to size and price an order safely.

        A missing filter is not an absent limit: it means we did not read one.
        The risk engine treats an incomplete set as a refusal, never as freedom.
        """
        return self.price is not None and self.lot_size is not None and self.notional is not None


def _decimal_field(raw: dict[str, Any], key: str, *, default: str = "0") -> Decimal:
    value = raw.get(key, default)
    if isinstance(value, float):
        # The exchange sends strings. A float here means something upstream
        # already lost precision, and silently accepting it would hide that.
        raise SymbolFilterError(
            f"Filter field {key!r} arrived as a float ({value!r}); expected a string."
        )
    try:
        return to_decimal(value)
    except Exception as exc:
        raise SymbolFilterError(f"Filter field {key!r} is not a number: {value!r}") from exc


def parse_symbol_filters(symbol: str, raw_filters: list[dict[str, Any]]) -> SymbolFilters:
    """Turn the exchange's filter list into typed values.

    Unknown filter types are ignored on purpose: the exchange adds new ones, and
    refusing to start because of a filter we do not use yet would be a denial of
    service we inflicted on ourselves. Filters we DO rely on are validated.
    """
    price: PriceFilter | None = None
    lot_size: LotSizeFilter | None = None
    notional: NotionalFilter | None = None

    for raw in raw_filters:
        filter_type = raw.get("filterType")

        if filter_type == "PRICE_FILTER":
            price = PriceFilter(
                min_price=_decimal_field(raw, "minPrice"),
                max_price=_decimal_field(raw, "maxPrice"),
                tick_size=_decimal_field(raw, "tickSize"),
            )
        elif filter_type == "LOT_SIZE":
            lot_size = LotSizeFilter(
                min_quantity=_decimal_field(raw, "minQty"),
                max_quantity=_decimal_field(raw, "maxQty"),
                step_size=_decimal_field(raw, "stepSize"),
            )
        elif filter_type == "NOTIONAL":
            # The richer of the two: has both bounds.
            notional = NotionalFilter(
                min_notional=_decimal_field(raw, "minNotional"),
                max_notional=_decimal_field(raw, "maxNotional"),
                applies_to_market_orders=bool(raw.get("applyMinToMarket", False)),
            )
        elif filter_type == "MIN_NOTIONAL" and notional is None:
            # The legacy filter, used only when NOTIONAL is absent: it has no
            # upper bound, so it must not overwrite the richer one.
            notional = NotionalFilter(
                min_notional=_decimal_field(raw, "minNotional"),
                max_notional=Decimal(0),
                applies_to_market_orders=bool(raw.get("applyToMarket", False)),
            )

    return SymbolFilters(symbol=symbol, price=price, lot_size=lot_size, notional=notional)
