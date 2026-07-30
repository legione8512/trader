"""Position sizing.

The invariant under test throughout: whatever the rounding, the clamping and the
filters do, the money at risk never exceeds the budget. Several tests assert it
over generated inputs rather than one example, because the failure mode is a
combination nobody thought to write down.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.enums import OrderSide, RiskReasonCode
from app.domain.position_sizing import (
    PositionSizingError,
    SizingRequest,
    size_position,
)
from app.domain.symbol_filters import (
    LotSizeFilter,
    NotionalFilter,
    PriceFilter,
    SymbolFilters,
)

#: Shaped after a real BTCUSDT filter set: a 0.01 tick, a 1e-5 lot step and a
#: 5 USDT minimum notional.
BTC_FILTERS = SymbolFilters(
    symbol="BTCUSDT",
    price=PriceFilter(
        min_price=Decimal("0.01"), max_price=Decimal("1000000"), tick_size=Decimal("0.01")
    ),
    lot_size=LotSizeFilter(
        min_quantity=Decimal("0.00001"),
        max_quantity=Decimal("9000"),
        step_size=Decimal("0.00001"),
    ),
    notional=NotionalFilter(
        min_notional=Decimal("5"), max_notional=Decimal("9000000"), applies_to_market_orders=True
    ),
)

#: RON per USDT. Locked per trading day (decision OD-02).
FUNDING_RATE = Decimal("4.60")
#: 0.50% of 1000 RON.
RISK_BUDGET = Decimal("5.00")


def _with_tick(tick: Decimal) -> SymbolFilters:
    """The standard filter set with a different price tick."""
    return SymbolFilters(
        symbol="BTCUSDT",
        price=PriceFilter(min_price=Decimal("0"), max_price=Decimal("0"), tick_size=tick),
        lot_size=BTC_FILTERS.lot_size,
        notional=BTC_FILTERS.notional,
    )


def request(**overrides: object) -> SizingRequest:
    values: dict[str, object] = {
        "side": OrderSide.BUY,
        "reference_price": Decimal("65000.00"),
        "stop_loss_price": Decimal("64350.00"),
        "take_profit_price": Decimal("66625.00"),
        "risk_budget_reporting": RISK_BUDGET,
        "funding_rate": FUNDING_RATE,
        "filters": BTC_FILTERS,
    }
    values.update(overrides)
    return SizingRequest(**values)  # type: ignore[arg-type]


class TestTheInvariant:
    def test_the_risk_never_exceeds_the_budget(self) -> None:
        result = size_position(request())
        assert result.is_viable
        assert result.risk_reporting <= RISK_BUDGET

    @pytest.mark.parametrize("price", ["100", "3500.55", "65000", "0.00004321", "1.2345"])
    @pytest.mark.parametrize("stop_fraction", ["0.005", "0.0123", "0.03"])
    def test_the_risk_never_exceeds_the_budget_across_prices_and_stops(
        self, price: str, stop_fraction: str
    ) -> None:
        """The failure mode is a combination, not an example."""
        reference = Decimal(price)
        stop = reference * (1 - Decimal(stop_fraction))
        result = size_position(
            request(reference_price=reference, stop_loss_price=stop, take_profit_price=None)
        )
        # Viable or not, the number reported must never exceed the budget.
        assert result.risk_reporting <= RISK_BUDGET

    def test_the_reported_risk_is_recomputed_from_the_final_numbers(self) -> None:
        """Not carried forward from before the rounding."""
        result = size_position(request())
        assert result.is_viable
        expected = result.quantity * result.stop_distance * FUNDING_RATE
        assert result.risk_reporting == expected

    def test_rounding_the_quantity_down_leaves_budget_unspent(self) -> None:
        """Routine, and worth seeing: it is how an operator notices a lot step
        too coarse for the budget."""
        result = size_position(request())
        assert result.is_viable
        assert result.risk_utilisation <= 1
        assert result.risk_utilisation > Decimal("0.99")


class TestRoundingDirections:
    def test_a_long_entry_rounds_down_to_the_tick(self) -> None:
        """We never pay more than the strategy's reference price."""
        result = size_position(request(reference_price=Decimal("65000.019")))
        assert result.entry_price == Decimal("65000.01")

    def test_a_short_entry_rounds_up_to_the_tick(self) -> None:
        result = size_position(
            request(
                side=OrderSide.SELL,
                reference_price=Decimal("65000.011"),
                stop_loss_price=Decimal("65650.00"),
                take_profit_price=Decimal("63375.00"),
            )
        )
        assert result.entry_price == Decimal("65000.02")

    def test_a_long_stop_rounds_away_from_the_entry(self) -> None:
        """Snapping it toward the entry would tighten the invalidation level the
        strategy chose, without anyone deciding to."""
        result = size_position(request(stop_loss_price=Decimal("64350.019")))
        assert result.stop_loss_price == Decimal("64350.01")
        assert result.stop_loss_price < result.entry_price

    def test_a_short_stop_rounds_away_from_the_entry(self) -> None:
        result = size_position(
            request(
                side=OrderSide.SELL,
                reference_price=Decimal("65000.00"),
                stop_loss_price=Decimal("65650.011"),
                take_profit_price=Decimal("63375.00"),
            )
        )
        assert result.stop_loss_price == Decimal("65650.02")
        assert result.stop_loss_price > result.entry_price

    def test_a_long_target_rounds_toward_the_entry(self) -> None:
        """A tick earlier rather than a tick later: it gives up a fraction of a
        tick and buys a materially better chance of filling."""
        result = size_position(request(take_profit_price=Decimal("66625.019")))
        assert result.take_profit_price == Decimal("66625.01")

    def test_rounding_never_moves_the_stop_across_the_entry(self) -> None:
        """The bug this guards against: with the direction chosen by comparing
        against the ALREADY-ROUNDED entry, a long whose entry rounded down past
        its stop had the stop rounded further up, inverting the position while
        still reporting a risk inside the budget."""
        for tick in ("0.01", "0.5", "1", "10"):
            result = size_position(
                request(
                    reference_price=Decimal("65000.5"),
                    stop_loss_price=Decimal("64990.2"),
                    take_profit_price=None,
                    filters=_with_tick(Decimal(tick)),
                )
            )
            if result.is_viable:
                assert result.stop_loss_price < result.entry_price, f"inverted at tick {tick}"

    def test_widening_the_stop_by_rounding_does_not_overspend(self) -> None:
        """The position is sized from the WIDENED distance, so the money at
        risk stays inside the budget rather than growing with it."""
        result = size_position(request(stop_loss_price=Decimal("64350.019")))
        assert result.is_viable
        assert result.risk_reporting <= RISK_BUDGET


class TestUnits:
    def test_the_budget_is_converted_at_the_locked_funding_rate(self) -> None:
        result = size_position(request())
        assert result.is_viable
        # quantity ~= (5 RON / 4.60) / 650 USDT
        expected_quote_budget = RISK_BUDGET / FUNDING_RATE
        assert result.risk_quote <= expected_quote_budget

    def test_both_currencies_are_reported(self) -> None:
        """A report must never have to guess which currency it is looking at."""
        result = size_position(request())
        assert result.risk_quote > 0
        assert result.risk_reporting > 0
        assert result.risk_reporting != result.risk_quote

    def test_a_different_funding_rate_changes_the_quantity(self) -> None:
        cheap = size_position(request(funding_rate=Decimal("4.00")))
        dear = size_position(request(funding_rate=Decimal("5.00")))
        assert cheap.quantity > dear.quantity


class TestRefusals:
    def test_a_position_below_the_minimum_notional_is_refused_not_enlarged(self) -> None:
        """The single most important refusal here. Raising the quantity until it
        clears would spend more than the approved risk."""
        tiny_budget = size_position(
            request(risk_budget_reporting=Decimal("0.02"), take_profit_price=None)
        )
        assert not tiny_budget.is_viable
        assert RiskReasonCode.MIN_NOTIONAL_NOT_MET in tiny_budget.reason_codes
        assert tiny_budget.quantity == 0

    def test_a_quantity_below_the_lot_minimum_is_refused(self) -> None:
        coarse = SymbolFilters(
            symbol="BTCUSDT",
            price=BTC_FILTERS.price,
            lot_size=LotSizeFilter(
                min_quantity=Decimal("1"), max_quantity=Decimal("9000"), step_size=Decimal("1")
            ),
            notional=BTC_FILTERS.notional,
        )
        result = size_position(request(filters=coarse, take_profit_price=None))
        assert not result.is_viable
        assert RiskReasonCode.EXCHANGE_FILTER_VIOLATION in result.reason_codes

    def test_an_incomplete_filter_set_is_a_refusal_never_a_freedom(self) -> None:
        """A missing filter means we never read one, not that no limit exists."""
        incomplete = SymbolFilters(symbol="BTCUSDT", price=BTC_FILTERS.price)
        result = size_position(request(filters=incomplete))
        assert not result.is_viable
        assert RiskReasonCode.EXCHANGE_FILTER_VIOLATION in result.reason_codes

    def test_a_balance_too_small_to_pay_for_the_position_is_refused(self) -> None:
        result = size_position(
            request(available_quote_balance=Decimal("1"), take_profit_price=None)
        )
        assert not result.is_viable
        assert RiskReasonCode.MIN_NOTIONAL_NOT_MET in result.reason_codes

    def test_a_stop_inside_the_same_tick_as_the_entry_is_refused(self) -> None:
        """Sizing against a zero distance would divide by zero; sizing against a
        'nearly zero' one would produce an unbounded position.

        It happens when the entry and the stop land in the same tick bucket,
        which a coarse tick makes easy.
        """
        result = size_position(
            request(
                reference_price=Decimal("65000.5"),
                stop_loss_price=Decimal("65000.2"),
                take_profit_price=None,
                filters=_with_tick(Decimal("1")),
            )
        )
        assert not result.is_viable
        assert RiskReasonCode.EXCHANGE_FILTER_VIOLATION in result.reason_codes

    def test_a_coarse_tick_widens_the_stop_and_shrinks_the_position(self) -> None:
        """Not a refusal: the widened stop is honoured and the size drops to
        match, so the money at risk is unchanged. This is the designed
        behaviour of rounding the stop away, and it needs to be visible."""
        fine = size_position(
            request(
                reference_price=Decimal("65000"),
                stop_loss_price=Decimal("64990"),
                take_profit_price=None,
            )
        )
        coarse = size_position(
            request(
                reference_price=Decimal("65000"),
                stop_loss_price=Decimal("64990"),
                take_profit_price=None,
                filters=_with_tick(Decimal("1000")),
            )
        )
        assert fine.is_viable and coarse.is_viable
        assert coarse.stop_distance > fine.stop_distance
        assert coarse.quantity < fine.quantity
        assert coarse.risk_reporting <= RISK_BUDGET

    def test_a_refusal_still_explains_itself(self) -> None:
        """A refusal nobody can explain to an operator is not a refusal, it is
        a shrug."""
        result = size_position(
            request(risk_budget_reporting=Decimal("0.02"), take_profit_price=None)
        )
        assert not result.is_viable
        assert "min_notional" in result.detail
        assert "notional_quote" in result.detail
        assert result.risk_budget_reporting == Decimal("0.02")


class TestClamping:
    def test_the_maximum_lot_size_clamps_rather_than_refuses(self) -> None:
        """A smaller position is always allowed, so a ceiling is not a refusal."""
        capped = SymbolFilters(
            symbol="BTCUSDT",
            price=BTC_FILTERS.price,
            lot_size=LotSizeFilter(
                min_quantity=Decimal("0.00001"),
                max_quantity=Decimal("0.00002"),
                step_size=Decimal("0.00001"),
            ),
            notional=NotionalFilter(
                min_notional=Decimal("0.5"),
                max_notional=Decimal("9000000"),
                applies_to_market_orders=True,
            ),
        )
        result = size_position(request(filters=capped, take_profit_price=None))
        assert result.is_viable
        assert result.quantity == Decimal("0.00002")

    def test_the_maximum_notional_clamps_the_quantity(self) -> None:
        capped = SymbolFilters(
            symbol="BTCUSDT",
            price=BTC_FILTERS.price,
            lot_size=BTC_FILTERS.lot_size,
            notional=NotionalFilter(
                min_notional=Decimal("5"),
                max_notional=Decimal("60"),
                applies_to_market_orders=True,
            ),
        )
        result = size_position(request(filters=capped, take_profit_price=None))
        assert result.is_viable
        assert result.notional_quote <= Decimal("60")

    def test_a_balance_ceiling_clamps_before_it_refuses(self) -> None:
        """Buy what can be paid for, as long as that still clears the minimum."""
        result = size_position(
            request(available_quote_balance=Decimal("50"), take_profit_price=None)
        )
        assert result.is_viable
        assert result.notional_quote <= Decimal("50")

    def test_a_clamped_position_still_respects_the_risk_budget(self) -> None:
        result = size_position(
            request(available_quote_balance=Decimal("50"), take_profit_price=None)
        )
        assert result.risk_reporting <= RISK_BUDGET


class TestMalformedRequests:
    def test_a_long_with_the_stop_above_the_entry_is_a_programming_error(self) -> None:
        """Not an unviable trade - an inverted sign. It raises rather than
        returning a refusal, because there is nothing to explain to an operator."""
        with pytest.raises(PositionSizingError, match="long stop must be below"):
            request(stop_loss_price=Decimal("66000"))

    def test_a_short_with_the_stop_below_the_entry_is_refused(self) -> None:
        with pytest.raises(PositionSizingError, match="short stop must be above"):
            request(side=OrderSide.SELL, stop_loss_price=Decimal("64000"))

    def test_a_non_positive_budget_is_refused(self) -> None:
        with pytest.raises(PositionSizingError, match="Risk budget must be positive"):
            request(risk_budget_reporting=Decimal(0))

    def test_a_non_positive_funding_rate_is_refused(self) -> None:
        """Zero would divide by zero; negative would invert the position."""
        with pytest.raises(PositionSizingError, match="Funding rate must be positive"):
            request(funding_rate=Decimal(0))


class TestOutputPrecision:
    def test_prices_are_emitted_at_the_precision_the_system_stores(self) -> None:
        result = size_position(request())
        assert result.is_viable
        for price in (result.entry_price, result.stop_loss_price, result.take_profit_price):
            assert price is not None
            exponent = price.as_tuple().exponent
            assert isinstance(exponent, int)
            assert -exponent <= 12

    def test_the_quantity_lands_exactly_on_the_lot_step(self) -> None:
        """Anything else is rejected by the exchange."""
        result = size_position(request())
        assert result.is_viable
        assert BTC_FILTERS.lot_size is not None
        assert BTC_FILTERS.lot_size.is_satisfied_by(result.quantity)

    def test_the_prices_satisfy_the_price_filter(self) -> None:
        result = size_position(request())
        assert result.is_viable
        assert BTC_FILTERS.price is not None
        assert BTC_FILTERS.price.is_satisfied_by(result.entry_price)
        assert BTC_FILTERS.price.is_satisfied_by(result.stop_loss_price)
