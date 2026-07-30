"""Turning a risk budget into a quantity the exchange will accept.

This is the most dangerous arithmetic in the application. Every step of it can
be wrong in a way that looks right, so each rounding direction is chosen
deliberately and stated.

**The invariant.** After every rounding, clamping and filter check, the money at
risk must still be no more than the budget. It is recomputed from the final
numbers and checked, rather than assumed to have survived. Rounding down at each
step makes the check nearly always pass - which is exactly why it must still be
performed: a check that never fires is the one that catches the case nobody
thought of.

**The refusal that matters most.** When the position is too small to clear the
exchange's minimum notional, the answer is to refuse the trade. It is never to
raise the quantity until it clears, because that spends more than the approved
risk. A trade that cannot be taken at the approved size is a trade that is not
taken.

**Units.** The risk budget is in the reporting currency (RON); prices and
notionals are in the venue's quote currency (USDT). They are converted once, at
the locked funding rate (decision OD-02), and the result carries both so a
report never has to guess which one it is looking at.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, localcontext

from app.domain.enums import OrderSide, RiskReasonCode
from app.domain.errors import DomainError
from app.domain.money import CALCULATION_PRECISION, quantize_price
from app.domain.symbol_filters import SymbolFilters

ZERO = Decimal(0)


class PositionSizingError(DomainError):
    """The sizing request itself is malformed, not merely unviable."""


@dataclass(frozen=True, slots=True)
class SizingRequest:
    """Everything needed to size one position."""

    side: OrderSide
    #: The price the order will be placed at, before tick rounding.
    reference_price: Decimal
    #: The invalidation level, before tick rounding.
    stop_loss_price: Decimal
    #: Risk budget in the reporting currency, already computed from
    #: ``maximumRiskPerTradePercent`` of the reference capital.
    risk_budget_reporting: Decimal
    #: Reporting currency per one unit of quote currency. Locked per trading day
    #: (decision OD-02), never re-read mid-day.
    funding_rate: Decimal
    filters: SymbolFilters
    take_profit_price: Decimal | None = None
    #: Quote currency actually available. ``None`` means "not checked here",
    #: which is honest: the balance check then has to happen somewhere else.
    available_quote_balance: Decimal | None = None

    def __post_init__(self) -> None:
        if self.reference_price <= ZERO:
            raise PositionSizingError(f"Reference price must be positive: {self.reference_price}")
        if self.stop_loss_price <= ZERO:
            raise PositionSizingError(f"Stop price must be positive: {self.stop_loss_price}")
        if self.risk_budget_reporting <= ZERO:
            raise PositionSizingError(f"Risk budget must be positive: {self.risk_budget_reporting}")
        if self.funding_rate <= ZERO:
            raise PositionSizingError(f"Funding rate must be positive: {self.funding_rate}")
        if self.side is OrderSide.BUY and self.stop_loss_price >= self.reference_price:
            raise PositionSizingError("A long stop must be below the entry")
        if self.side is OrderSide.SELL and self.stop_loss_price <= self.reference_price:
            raise PositionSizingError("A short stop must be above the entry")


@dataclass(frozen=True, slots=True)
class SizingResult:
    """What may be ordered, or why nothing may be.

    Both outcomes carry the intermediate numbers. An approval that cannot be
    explained is as useless as a refusal that cannot be.
    """

    is_viable: bool
    reason_codes: tuple[RiskReasonCode, ...] = ()

    quantity: Decimal = ZERO
    #: Prices after tick rounding: what will actually be sent.
    entry_price: Decimal = ZERO
    stop_loss_price: Decimal = ZERO
    take_profit_price: Decimal | None = None

    #: Distance the position is sized against, after rounding.
    stop_distance: Decimal = ZERO
    notional_quote: Decimal = ZERO
    risk_quote: Decimal = ZERO
    risk_reporting: Decimal = ZERO
    #: The budget this was sized against, for comparison in a report.
    risk_budget_reporting: Decimal = ZERO
    detail: dict[str, str] = field(default_factory=dict)

    @property
    def risk_utilisation(self) -> Decimal:
        """Fraction of the budget actually used.

        Routinely well below 1: rounding the quantity down to the lot step
        leaves the remainder unspent. That is the safe direction, and seeing how
        much is left is how an operator notices a lot step too coarse for the
        budget.
        """
        if self.risk_budget_reporting <= ZERO:
            return ZERO
        with localcontext() as arithmetic:
            arithmetic.prec = CALCULATION_PRECISION
            return self.risk_reporting / self.risk_budget_reporting


def _refuse(
    request: SizingRequest, codes: tuple[RiskReasonCode, ...], **detail: str
) -> SizingResult:
    return SizingResult(
        is_viable=False,
        reason_codes=codes,
        risk_budget_reporting=request.risk_budget_reporting,
        detail=detail,
    )


def size_position(request: SizingRequest) -> SizingResult:
    """Compute the largest quantity that respects every limit at once."""
    with localcontext() as arithmetic:
        arithmetic.prec = CALCULATION_PRECISION
        return _size(request)


def _size(request: SizingRequest) -> SizingResult:
    filters = request.filters
    if not filters.is_complete_for_trading:
        # A missing filter is not an absent limit; it means we never read one.
        # Treating it as freedom is how an order gets rejected by the venue at
        # the worst possible moment.
        return _refuse(
            request,
            (RiskReasonCode.EXCHANGE_FILTER_VIOLATION,),
            missing_filters="price, lot size or notional filter was not available",
        )

    price_filter = filters.price
    lot_filter = filters.lot_size
    notional_filter = filters.notional
    assert price_filter is not None and lot_filter is not None and notional_filter is not None

    is_long = request.side is OrderSide.BUY

    # --------------------------------------------------------------- prices ---
    # The entry moves in our favour: a long buys no higher than the strategy's
    # reference, a short sells no lower.
    entry_price = (
        price_filter.round_price_down(request.reference_price)
        if is_long
        else price_filter.round_price_up(request.reference_price)
    )
    # The stop moves AWAY from the entry, and the direction comes from the SIDE
    # rather than from comparing against the rounded entry. Comparing was tried
    # and was wrong: when the entry rounds down past the stop, the stop compares
    # as "above" and gets rounded further up, landing on the wrong side of the
    # entry and inverting the position.
    #
    # Away, not toward: snapping a stop toward the entry tightens the
    # invalidation level the strategy chose and makes the position more likely
    # to be closed by noise, without anyone deciding to. Widening is safe,
    # because the position is then sized from the widened distance.
    stop_price = (
        price_filter.round_price_down(request.stop_loss_price)
        if is_long
        else price_filter.round_price_up(request.stop_loss_price)
    )

    if entry_price <= ZERO or stop_price <= ZERO:
        return _refuse(
            request,
            (RiskReasonCode.EXCHANGE_FILTER_VIOLATION,),
            rounding="tick rounding produced a non-positive price",
        )
    if not price_filter.is_satisfied_by(entry_price):
        return _refuse(
            request,
            (RiskReasonCode.EXCHANGE_FILTER_VIOLATION,),
            entry_price=str(entry_price),
            price_filter="entry price outside the exchange bounds after rounding",
        )

    # Rounding must never move the stop across the entry. With the direction
    # taken from the side this cannot happen - which is exactly why the check
    # stays: it is the one that catches the case nobody anticipated.
    if (is_long and stop_price >= entry_price) or (not is_long and stop_price <= entry_price):
        return _refuse(
            request,
            (RiskReasonCode.EXCHANGE_FILTER_VIOLATION,),
            entry_price=str(entry_price),
            stop_loss_price=str(stop_price),
            rounding="tick rounding put the stop on the wrong side of the entry",
        )

    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= ZERO:
        # Possible when the tick is coarse relative to the stop the strategy
        # chose. Sizing against a zero distance would divide by zero; sizing
        # against a "small" one would produce an unbounded position.
        return _refuse(
            request,
            (RiskReasonCode.EXCHANGE_FILTER_VIOLATION,),
            stop_distance="tick rounding collapsed the stop onto the entry",
        )

    # ------------------------------------------------------- risk in quote ---
    risk_quote_budget = request.risk_budget_reporting / request.funding_rate

    # ------------------------------------------------------------ quantity ---
    raw_quantity = risk_quote_budget / stop_distance
    quantity = lot_filter.round_quantity(raw_quantity)

    # An upper lot bound is a hard ceiling, so clamp rather than refuse: a
    # smaller position is always allowed. Re-rounded, because maxQty need not
    # itself be a multiple of the step.
    if lot_filter.max_quantity > ZERO and quantity > lot_filter.max_quantity:
        quantity = lot_filter.round_quantity(lot_filter.max_quantity)

    # The maximum notional is the same kind of ceiling, expressed in money.
    if notional_filter.max_notional > ZERO:
        affordable = notional_filter.max_notional / entry_price
        if quantity > affordable:
            quantity = lot_filter.round_quantity(affordable)

    # A balance ceiling behaves identically: buy what can be paid for.
    if request.available_quote_balance is not None:
        affordable = request.available_quote_balance / entry_price
        if quantity > affordable:
            quantity = lot_filter.round_quantity(affordable)

    # ------------------------------------------------ post-clamp validation ---
    codes: list[RiskReasonCode] = []
    notional = quantity * entry_price

    if quantity <= ZERO or quantity < lot_filter.min_quantity:
        # Never rounded up to reach the minimum: that would spend more than the
        # approved risk. A trade that cannot be taken at the approved size is a
        # trade that is not taken.
        codes.append(RiskReasonCode.EXCHANGE_FILTER_VIOLATION)
    if not notional_filter.is_satisfied_by(notional):
        codes.append(RiskReasonCode.MIN_NOTIONAL_NOT_MET)
    if request.available_quote_balance is not None and notional > request.available_quote_balance:
        codes.append(RiskReasonCode.INSUFFICIENT_BALANCE)

    # --------------------------------------------- the invariant, rechecked ---
    # Recomputed from the FINAL numbers rather than carried forward. Rounding
    # down makes this pass almost always, which is precisely why it is still
    # performed: the check that never fires is the one that catches the case
    # nobody anticipated.
    risk_quote = quantity * stop_distance
    risk_reporting = risk_quote * request.funding_rate
    if risk_reporting > request.risk_budget_reporting:
        codes.append(RiskReasonCode.RISK_PER_TRADE_EXCEEDED)

    detail = {
        "entry_price": str(entry_price),
        "stop_loss_price": str(stop_price),
        "stop_distance": str(stop_distance),
        "raw_quantity": str(raw_quantity),
        "quantity": str(quantity),
        "notional_quote": str(notional),
        "risk_quote": str(risk_quote),
        "risk_reporting": str(risk_reporting),
        "risk_budget_reporting": str(request.risk_budget_reporting),
        "funding_rate": str(request.funding_rate),
        "min_quantity": str(lot_filter.min_quantity),
        "min_notional": str(notional_filter.min_notional),
    }

    if codes:
        return _refuse(request, tuple(codes), **detail)

    # The target moves TOWARD the entry: a long takes profit a tick earlier
    # rather than a tick later. It gives up a fraction of a tick and buys a
    # materially better chance of the order filling at all.
    take_profit = None
    if request.take_profit_price is not None:
        take_profit = (
            price_filter.round_price_down(request.take_profit_price)
            if is_long
            else price_filter.round_price_up(request.take_profit_price)
        )
        if (is_long and take_profit <= entry_price) or (not is_long and take_profit >= entry_price):
            return _refuse(
                request,
                (RiskReasonCode.EXCHANGE_FILTER_VIOLATION,),
                take_profit="tick rounding collapsed the target onto the entry",
                **detail,
            )

    return SizingResult(
        is_viable=True,
        quantity=quantity,
        entry_price=quantize_price(entry_price),
        stop_loss_price=quantize_price(stop_price),
        take_profit_price=quantize_price(take_profit) if take_profit is not None else None,
        stop_distance=quantize_price(stop_distance),
        notional_quote=notional,
        risk_quote=risk_quote,
        risk_reporting=risk_reporting,
        risk_budget_reporting=request.risk_budget_reporting,
        detail=detail,
    )
