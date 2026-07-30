"""Everything the risk engine is allowed to look at.

One explicit structure rather than a set of collaborators the rules can query.
The difference matters: a rule that can call out to fetch what it needs can be
non-deterministic, can be slow, and can see a different world than the rule
evaluated a microsecond earlier. A rule that reads a frozen value object sees
exactly what every other rule sees, and the whole assessment describes one
instant.

It is also what makes the record honest. Every field here is written into the
``RiskAssessment``, so the stored explanation is the actual input, not a
reconstruction.

Assembling this from the database, the exchange and the clock is the application
layer's job. Nothing in this package knows those exist.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from app.domain.enums import HealthStatus, OrderSide
from app.domain.errors import DomainError
from app.domain.risk.limits import RiskLimits

ZERO = Decimal(0)


class RiskContextError(DomainError):
    """The context is not usable as given."""


@dataclass(frozen=True, slots=True)
class ProposalUnderReview:
    """The trade being judged, reduced to what the rules need.

    Deliberately not ``SignalProposal``. The risk engine must not depend on the
    strategy layer: it judges a proposal, it does not care which kind of code
    produced one, and a backtest replaying stored rows has no strategy object to
    hand it.
    """

    side: OrderSide
    entry_price: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal | None = None
    #: Reward-to-risk AFTER estimated fees and slippage (rule R-14). Gross is
    #: not accepted here: it is the number that flatters, and the rule that
    #: matters is about the number that does not.
    net_reward_risk_ratio: Decimal | None = None
    #: What sizing concluded. ``None`` when sizing has not run, which is itself
    #: a reason to refuse rather than to assume.
    quantity: Decimal | None = None
    risk_amount_reporting: Decimal | None = None
    notional_quote: Decimal | None = None
    #: Age of the signal at evaluation time (rule R-10).
    signal_age: timedelta | None = None


@dataclass(frozen=True, slots=True)
class DayState:
    """Where the trading day stands."""

    #: Positive is profit. R-26 decides which of the two the daily loss limit
    #: is measured against; both are carried so the record shows both.
    realised_pnl: Decimal = ZERO
    unrealised_pnl: Decimal = ZERO
    #: Highest total P&L reached today, for R-23.
    peak_pnl: Decimal = ZERO
    open_positions: int = 0
    trades_today: int = 0
    consecutive_losses: int = 0
    session_pnl: Decimal = ZERO
    #: Time left before the trading day ends (R-24). Days are 23, 24 or 25
    #: hours long in Europe/Bucharest, so this is passed in rather than derived
    #: from a fixed length.
    time_remaining_in_day: timedelta | None = None

    @property
    def total_pnl(self) -> Decimal:
        return self.realised_pnl + self.unrealised_pnl


@dataclass(frozen=True, slots=True)
class MarketState:
    """Observed market quality. ``None`` means "not measured".

    A missing measurement is never read as a good one. The rule that needs it
    refuses, on the same principle as an uncalibrated threshold: not knowing is
    not the same as being fine.
    """

    candle_age: timedelta | None = None
    spread_bps: Decimal | None = None
    order_book_depth_quote: Decimal | None = None
    atr_percent: Decimal | None = None
    estimated_slippage_bps: Decimal | None = None
    clock_drift_ms: Decimal | None = None


@dataclass(frozen=True, slots=True)
class SystemState:
    """Whether the machinery is fit to trade at all."""

    health: HealthStatus = HealthStatus.HEALTHY
    exchange_healthy: bool = True
    emergency_stop_active: bool = False
    strategy_enabled: bool = True
    within_trading_window: bool = True
    #: Whether sizing produced a viable position and which filters it violated.
    sizing_is_viable: bool = True
    sizing_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RiskContext:
    """One instant, frozen, as every rule will see it."""

    evaluated_at: datetime
    limits: RiskLimits
    day: DayState = DayState()
    market: MarketState = MarketState()
    system: SystemState = SystemState()
    #: ``None`` for a day- or session-level gate, for example "may a session
    #: start now". Rules that need a proposal skip cleanly rather than assuming
    #: an empty one.
    proposal: ProposalUnderReview | None = None

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None:
            raise RiskContextError("evaluated_at must be timezone-aware")

    @property
    def is_proposal_review(self) -> bool:
        return self.proposal is not None
