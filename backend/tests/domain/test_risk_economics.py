"""Trading costs, and the ratio rule R-14 actually judges."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.domain.risk.economics import (
    TradingCostError,
    TradingCosts,
    cost_in_r,
    net_reward_risk_ratio,
)

#: Binance spot, regular user, verified from the published schedule: 0.100% per
#: side, or 0.075% when fees are paid in BNB (decision OD-16).
STANDARD = TradingCosts(fee_rate_per_side=Decimal("0.001"))
WITH_BNB = TradingCosts(fee_rate_per_side=Decimal("0.00075"))


class TestRoundTripCost:
    def test_the_fee_is_charged_on_both_legs(self) -> None:
        """A round trip pays it twice. Counting one leg halves the cost of
        every trade in the model and none of it in reality."""
        assert STANDARD.round_trip_fraction == Decimal("0.002")

    def test_the_bnb_discount_is_a_quarter_off(self) -> None:
        assert WITH_BNB.round_trip_fraction == Decimal("0.0015")

    def test_slippage_adds_to_the_round_trip(self) -> None:
        costs = TradingCosts(
            fee_rate_per_side=Decimal("0.001"), estimated_slippage_bps=Decimal("5")
        )
        # 5 bps = 0.0005 per side, so 0.001 + 0.0005 = 0.0015 per side.
        assert costs.round_trip_fraction == Decimal("0.003")

    def test_a_negative_fee_is_refused(self) -> None:
        with pytest.raises(TradingCostError, match="cannot be negative"):
            TradingCosts(fee_rate_per_side=Decimal("-0.001"))


class TestCostInR:
    def test_a_one_percent_stop_costs_a_fifth_of_r(self) -> None:
        """The number the whole strategy design turned on: 0.20R is the entire
        gross edge of a 2.0 reward target at a 40% win rate."""
        result = cost_in_r(
            entry_price=Decimal("100"), stop_loss_price=Decimal("99"), costs=STANDARD
        )
        assert result == Decimal("0.2")

    def test_a_wider_stop_costs_less_in_r(self) -> None:
        """Position size is risk divided by stop distance, so a tighter stop
        means a bigger position and a bigger fee measured in R."""
        tight = cost_in_r(
            entry_price=Decimal("100"), stop_loss_price=Decimal("99.5"), costs=STANDARD
        )
        wide = cost_in_r(entry_price=Decimal("100"), stop_loss_price=Decimal("98"), costs=STANDARD)
        assert tight > wide
        assert tight == Decimal("0.4")
        assert wide == Decimal("0.1")

    def test_paying_in_bnb_cuts_the_cost_by_a_quarter(self) -> None:
        standard = cost_in_r(
            entry_price=Decimal("100"), stop_loss_price=Decimal("99"), costs=STANDARD
        )
        discounted = cost_in_r(
            entry_price=Decimal("100"), stop_loss_price=Decimal("99"), costs=WITH_BNB
        )
        assert discounted == standard * Decimal("0.75")

    def test_a_zero_stop_distance_is_refused(self) -> None:
        with pytest.raises(TradingCostError, match="Stop distance"):
            cost_in_r(entry_price=Decimal("100"), stop_loss_price=Decimal("100"), costs=STANDARD)


class TestNetRewardRisk:
    def test_costs_reduce_the_reward_and_increase_the_loss(self) -> None:
        """Both, not just the first. A losing trade pays its fees too, and a
        model that only deducts them from the winner flatters every marginal
        setup."""
        gross_target = Decimal("2.5")
        entry = Decimal("100")
        stop = Decimal("99")
        target = entry + gross_target * (entry - stop)

        net = net_reward_risk_ratio(
            entry_price=entry, stop_loss_price=stop, take_profit_price=target, costs=STANDARD
        )
        # reward 2.5 - 0.2 = 2.3; risk 1 + 0.2 = 1.2; ratio 1.9166...
        assert net < gross_target
        assert net.quantize(Decimal("0.0001")) == Decimal("1.9167")

    def test_a_two_point_five_target_survives_the_fees_a_two_does_not(self) -> None:
        """The arithmetic behind decision OD-17, asserted rather than asserted
        about."""
        entry = Decimal("100")
        stop = Decimal("99")
        minimum = Decimal("1.8")

        approved = net_reward_risk_ratio(
            entry_price=entry,
            stop_loss_price=stop,
            take_profit_price=entry + Decimal("2.5") * (entry - stop),
            costs=STANDARD,
        )
        refused = net_reward_risk_ratio(
            entry_price=entry,
            stop_loss_price=stop,
            take_profit_price=entry + Decimal("2.0") * (entry - stop),
            costs=STANDARD,
        )
        assert approved >= minimum
        assert refused < minimum

    def test_a_negative_result_is_reported_not_clamped(self) -> None:
        """It says the costs exceed the entire reward, which is the most
        important thing the number has to say."""
        entry = Decimal("100")
        expensive = TradingCosts(
            fee_rate_per_side=Decimal("0.01"), estimated_slippage_bps=Decimal("50")
        )
        result = net_reward_risk_ratio(
            entry_price=entry,
            stop_loss_price=Decimal("99.9"),
            take_profit_price=Decimal("100.1"),
            costs=expensive,
        )
        assert result < 0

    def test_the_short_side_is_symmetric(self) -> None:
        long_ratio = net_reward_risk_ratio(
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("99"),
            take_profit_price=Decimal("102.5"),
            costs=STANDARD,
        )
        short_ratio = net_reward_risk_ratio(
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("101"),
            take_profit_price=Decimal("97.5"),
            costs=STANDARD,
        )
        assert long_ratio == short_ratio

    def test_zero_costs_recover_the_gross_ratio(self) -> None:
        free = TradingCosts(fee_rate_per_side=Decimal("0"))
        result = net_reward_risk_ratio(
            entry_price=Decimal("100"),
            stop_loss_price=Decimal("99"),
            take_profit_price=Decimal("102.5"),
            costs=free,
        )
        assert result == Decimal("2.5")
