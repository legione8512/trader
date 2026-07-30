"""The risk parameters, as a pure domain value.

A mirror of ``RiskConfiguration`` with no SQLAlchemy in it, so every rule can be
evaluated without a database. The mapping from the stored row lives in the
application layer; nothing here knows that a database exists.

Nullable thresholds mean one of two different things, and conflating them would
be a real safety bug:

* **Uncalibrated.** The value was never measured, because Phase 0 deliberately
  refused to guess it. The rule refuses.
* **Disabled.** A decision switched the rule off - R-23 by decision OD-03. The
  rule does not apply and does not block.

Which is which is a property of the rule, not of the value, so it is recorded on
the rule rather than inferred from a ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.domain.enums import DailyPnlBasis
from app.domain.errors import DomainError
from app.domain.money import percent_of

ZERO = Decimal(0)


class RiskLimitsError(DomainError):
    """The parameter set itself is not usable."""


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Every number the risk engine compares against."""

    # ------------------------------------------------------ fixed capital ---
    #: R-01. Never moves with profit or loss. This is what makes the model
    #: non-compounding: sizing references it and never the account equity.
    reference_capital: Decimal
    reporting_currency: str = "RON"

    # ------------------------------------------------------------- limits ---
    maximum_risk_per_trade_percent: Decimal = Decimal("0.50")
    daily_maximum_loss_percent: Decimal = Decimal("4.00")
    session_target_percent: Decimal = Decimal("2.00")
    session_restart_threshold_percent: Decimal = Decimal("4.00")
    maximum_open_positions: int = 1
    maximum_trades_per_day: int = 50
    maximum_consecutive_losses: int = 3
    no_new_entry_minutes_before_day_end: int = 30
    daily_pnl_basis: DailyPnlBasis = DailyPnlBasis.REALISED_PLUS_UNREALISED

    # ------------------------------------------- uncalibrated until phase 6 ---
    max_candle_age_seconds: int | None = None
    max_signal_age_seconds: int | None = None
    max_spread_bps: Decimal | None = None
    min_order_book_depth_quote: Decimal | None = None
    min_atr_percent: Decimal | None = None
    max_atr_percent: Decimal | None = None
    min_reward_risk_ratio: Decimal | None = None
    max_estimated_slippage_bps: Decimal | None = None
    max_clock_drift_ms: int | None = None

    # ------------------------------------------------ disabled by decision ---
    #: R-23. ``None`` means switched off by decision OD-03, not unmeasured.
    daily_profit_giveback_percent: Decimal | None = None

    def __post_init__(self) -> None:
        if self.reference_capital <= ZERO:
            raise RiskLimitsError("Reference capital must be positive")
        for name in (
            "maximum_risk_per_trade_percent",
            "daily_maximum_loss_percent",
            "session_target_percent",
            "session_restart_threshold_percent",
        ):
            value = getattr(self, name)
            if not (ZERO < value <= Decimal(100)):
                raise RiskLimitsError(f"{name} must be in (0, 100]: {value}")
        for name in (
            "maximum_open_positions",
            "maximum_trades_per_day",
            "maximum_consecutive_losses",
        ):
            if getattr(self, name) < 1:
                raise RiskLimitsError(f"{name} must be at least 1")
        if not (0 <= self.no_new_entry_minutes_before_day_end <= 1440):
            raise RiskLimitsError("no_new_entry_minutes_before_day_end must be within a day")

    # --------------------------------------------------- derived amounts ---
    #
    # All rounded DOWN. A budget rounded up is a budget exceeded, and these are
    # the numbers everything else is measured against.

    @property
    def risk_per_trade_amount(self) -> Decimal:
        """R-02. 0.50% of 1000 RON = 5.00 RON."""
        return percent_of(self.reference_capital, self.maximum_risk_per_trade_percent)

    @property
    def daily_maximum_loss_amount(self) -> Decimal:
        """R-03. Reported positive; compared against a negative P&L."""
        return percent_of(self.reference_capital, self.daily_maximum_loss_percent)

    @property
    def session_target_amount(self) -> Decimal:
        """R-07."""
        return percent_of(self.reference_capital, self.session_target_percent)

    @property
    def session_restart_threshold_amount(self) -> Decimal:
        """R-08."""
        return percent_of(self.reference_capital, self.session_restart_threshold_percent)

    def profit_giveback_amount(self, peak_profit: Decimal) -> Decimal | None:
        """R-23. ``None`` when the protection is switched off."""
        if self.daily_profit_giveback_percent is None:
            return None
        if peak_profit <= ZERO:
            return None
        return percent_of(peak_profit, self.daily_profit_giveback_percent)
