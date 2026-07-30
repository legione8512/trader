"""The cost of trading, applied to the numbers a rule judges.

Rule R-14 is explicit that the reward-to-risk ratio it checks is **net of fees
and slippage**. That distinction is the whole rule. A 2.5 gross ratio on a
position whose round trip costs 0.20R is not a 2.5 trade, and judging the gross
number would let through exactly the marginal trades the rule exists to stop.

The fee rate is configuration, never a constant in here. The published schedule
changes, it differs by VIP tier, and it differs again when fees are paid in BNB
(decision OD-16). A number hard-coded in this module would be a number nobody
re-checks.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from app.domain.errors import DomainError
from app.domain.money import CALCULATION_PRECISION

ZERO = Decimal(0)
#: One basis point as a fraction. 1 bp = 0.01% = 0.0001.
BASIS_POINT = Decimal("0.0001")


class TradingCostError(DomainError):
    """The cost model was given something it cannot use."""


@dataclass(frozen=True, slots=True)
class TradingCosts:
    """What one round trip costs, as fractions of notional."""

    #: Charged on entry AND on exit, so a round trip pays it twice.
    fee_rate_per_side: Decimal
    #: Expected adverse price movement between decision and fill, per side.
    estimated_slippage_bps: Decimal = ZERO

    def __post_init__(self) -> None:
        if self.fee_rate_per_side < ZERO:
            raise TradingCostError("Fee rate cannot be negative")
        if self.estimated_slippage_bps < ZERO:
            raise TradingCostError("Estimated slippage cannot be negative")

    @property
    def round_trip_fraction(self) -> Decimal:
        """Total cost of entering and exiting, as a fraction of notional."""
        return (self.fee_rate_per_side + self.estimated_slippage_bps * BASIS_POINT) * 2


def net_reward_risk_ratio(
    *,
    entry_price: Decimal,
    stop_loss_price: Decimal,
    take_profit_price: Decimal,
    costs: TradingCosts,
) -> Decimal:
    """Reward-to-risk after costs, per unit of position.

    Costs are charged on the notional, so they subtract from the reward and ADD
    to the loss. Both, not just the first: a losing trade pays its fees too, and
    a model that only deducts them from the winner flatters every marginal
    setup.

    Returned as a ratio and never clamped. A negative result is meaningful - it
    says the costs exceed the entire reward - and hiding it behind a floor of
    zero would erase the most important thing the number has to say.
    """
    if entry_price <= ZERO:
        raise TradingCostError("Entry price must be positive")

    with localcontext() as arithmetic:
        arithmetic.prec = CALCULATION_PRECISION

        risk_per_unit = abs(entry_price - stop_loss_price)
        if risk_per_unit <= ZERO:
            raise TradingCostError("Stop distance must be positive")
        reward_per_unit = abs(take_profit_price - entry_price)

        # Approximated at the entry price rather than computed against each exit
        # price separately. The difference is a second-order term on a move of a
        # few per cent, and the approximation is the CONSERVATIVE direction on
        # the loss leg, where the exit is below the entry for a long.
        cost_per_unit = entry_price * costs.round_trip_fraction

        net_reward = reward_per_unit - cost_per_unit
        net_risk = risk_per_unit + cost_per_unit
        return net_reward / net_risk


def cost_in_r(*, entry_price: Decimal, stop_loss_price: Decimal, costs: TradingCosts) -> Decimal:
    """Round-trip cost expressed in units of R.

    The number that decides whether a strategy can survive its own fees. At a
    stop 1% away from entry and a 0.200% round trip, it is 0.20R - which is the
    entire gross edge of a 2.0 reward target at a 40% win rate.
    """
    with localcontext() as arithmetic:
        arithmetic.prec = CALCULATION_PRECISION
        risk_per_unit = abs(entry_price - stop_loss_price)
        if risk_per_unit <= ZERO:
            raise TradingCostError("Stop distance must be positive")
        return (entry_price * costs.round_trip_fraction) / risk_per_unit
