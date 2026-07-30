"""From stored configuration to the pure domain value the rules read.

Kept as its own step rather than letting the rules read the ORM row. Two
reasons, and both are about being able to trust the result:

* the rules stay evaluable without a database, which is what makes the whole
  rule table testable against constructed instants;
* the P&L stored on a trading day is denominated in the **quote** currency while
  every limit is denominated in the **reporting** currency, and that conversion
  has to happen in exactly one place. Comparing a USDT loss against a RON limit
  would pass a day that had already breached it by a factor of four and a half.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, localcontext

from app.domain.errors import DomainError
from app.domain.money import CALCULATION_PRECISION
from app.domain.risk.context import DayState
from app.domain.risk.limits import RiskLimits
from app.persistence.models import RiskConfiguration, TradingDay, TradingSession

ZERO = Decimal(0)


class RiskMappingError(DomainError):
    """The stored configuration cannot be turned into usable limits."""


def limits_from_configuration(configuration: RiskConfiguration) -> RiskLimits:
    """Copy a stored configuration into the value the rules compare against.

    Field by field on purpose. A generic copy would silently pick up any column
    added later and hand the rules a value nobody reviewed.
    """
    return RiskLimits(
        reference_capital=configuration.reference_capital_ron,
        reporting_currency=configuration.reporting_currency,
        maximum_risk_per_trade_percent=configuration.maximum_risk_per_trade_percent,
        daily_maximum_loss_percent=configuration.daily_maximum_loss_percent,
        session_target_percent=configuration.session_target_percent,
        session_restart_threshold_percent=configuration.session_restart_threshold_percent,
        maximum_open_positions=configuration.maximum_open_positions,
        maximum_trades_per_day=configuration.maximum_trades_per_day,
        maximum_consecutive_losses=configuration.maximum_consecutive_losses,
        no_new_entry_minutes_before_day_end=configuration.no_new_entry_minutes_before_day_end,
        daily_pnl_basis=configuration.daily_pnl_basis,
        max_candle_age_seconds=configuration.max_candle_age_seconds,
        max_signal_age_seconds=configuration.max_signal_age_seconds,
        max_spread_bps=configuration.max_spread_bps,
        min_order_book_depth_quote=configuration.min_order_book_depth_quote,
        min_atr_percent=configuration.min_atr_percent,
        max_atr_percent=configuration.max_atr_percent,
        min_reward_risk_ratio=configuration.min_reward_risk_ratio,
        max_estimated_slippage_bps=configuration.max_estimated_slippage_bps,
        max_clock_drift_ms=configuration.max_clock_drift_ms,
        daily_profit_giveback_percent=configuration.daily_profit_giveback_percent,
    )


@dataclass(frozen=True, slots=True)
class DayStateSources:
    """The live figures that are not stored on the day row.

    Passed in rather than queried here, so the mapping stays a pure function of
    its inputs and a test can hand it a state that would take a day to produce.
    """

    open_positions: int
    consecutive_losses: int
    time_remaining_in_day: timedelta | None = None
    #: Unrealised P&L in the QUOTE currency, marked to the current price.
    #: Separate from the day row because that column is only as fresh as the
    #: last snapshot, and R-03 on the conservative basis needs it now.
    unrealised_pnl_quote: Decimal | None = None


def to_reporting(amount: Decimal, funding_rate: Decimal) -> Decimal:
    """Convert a quote-currency amount into the reporting currency.

    ``funding_rate`` is reporting per one unit of quote - RON per USDT - and is
    locked for the whole trading day by decision OD-02, so a day's numbers never
    shift because the exchange rate moved mid-afternoon.
    """
    if funding_rate <= ZERO:
        raise RiskMappingError(f"Funding rate must be positive: {funding_rate}")
    with localcontext() as arithmetic:
        arithmetic.prec = CALCULATION_PRECISION
        return amount * funding_rate


def day_state_from_records(
    day: TradingDay,
    sources: DayStateSources,
    session: TradingSession | None = None,
) -> DayState:
    """Assemble what the rules read about the current day.

    Everything monetary is converted into the reporting currency here, once.
    """
    rate = day.funding_rate_ron_per_quote
    unrealised_quote = (
        sources.unrealised_pnl_quote
        if sources.unrealised_pnl_quote is not None
        else day.unrealised_pnl_quote
    )
    return DayState(
        realised_pnl=to_reporting(day.realised_pnl_quote, rate),
        unrealised_pnl=to_reporting(unrealised_quote, rate),
        peak_pnl=to_reporting(day.peak_realised_pnl_quote, rate),
        open_positions=sources.open_positions,
        trades_today=day.trade_count,
        consecutive_losses=sources.consecutive_losses,
        # Zero rather than the day's total when no session is open: R-07 asks
        # whether THIS session hit its target, and answering with the day's
        # figure would close a session that had not earned anything.
        session_pnl=(
            to_reporting(session.realised_pnl_quote, rate) if session is not None else ZERO
        ),
        time_remaining_in_day=sources.time_remaining_in_day,
    )
