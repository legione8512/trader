"""Versioned configuration.

Configuration is **never modified in place**. A change creates a new version and
deactivates the previous one. A backtest run in March must be able to say
exactly which parameters produced its numbers, and a risk assessment stored last
week must still be explainable today.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.domain.enums import AutonomyMode, DailyPnlBasis, Timeframe
from app.persistence.base import Base
from app.persistence.mixins import CreatedAtMixin, UuidPrimaryKeyMixin
from app.persistence.types import currency_code, enum_column, fx_rate, monetary, percent


class RiskConfiguration(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Every risk parameter, versioned. See docs/RISK_RULES.md."""

    __tablename__ = "risk_configuration"
    __table_args__ = (
        # At most one ACTIVE configuration at any time. A partial unique index
        # over the rows where is_active is true makes that a database guarantee
        # rather than an application habit. Inactive versions stay unlimited,
        # which is the point: history is never deleted.
        Index(
            "uq_risk_configuration_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint("reference_capital_ron > 0", name="reference_capital_positive"),
        CheckConstraint(
            "session_target_percent > 0 AND session_target_percent <= 100",
            name="session_target_percent_range",
        ),
        CheckConstraint(
            "daily_maximum_loss_percent > 0 AND daily_maximum_loss_percent <= 100",
            name="daily_maximum_loss_percent_range",
        ),
        CheckConstraint(
            "maximum_risk_per_trade_percent > 0 AND maximum_risk_per_trade_percent <= 100",
            name="maximum_risk_per_trade_percent_range",
        ),
        CheckConstraint("maximum_open_positions >= 1", name="maximum_open_positions_min"),
        CheckConstraint("maximum_trades_per_day >= 1", name="maximum_trades_per_day_min"),
        CheckConstraint("maximum_consecutive_losses >= 1", name="maximum_consecutive_losses_min"),
        CheckConstraint(
            "no_new_entry_minutes_before_day_end >= 0 "
            "AND no_new_entry_minutes_before_day_end <= 1440",
            name="no_new_entry_minutes_range",
        ),
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ------------------------------------------------------- fixed capital ---
    #: R-01. Never changes with profit or loss. Variant A, no compounding.
    reference_capital_ron: Mapped[Decimal] = mapped_column(monetary(), nullable=False)
    reporting_currency: Mapped[str] = mapped_column(currency_code(), nullable=False, default="RON")

    # ------------------------------------------------------ session limits ---
    session_target_percent: Mapped[Decimal] = mapped_column(percent(), nullable=False)
    session_restart_threshold_percent: Mapped[Decimal] = mapped_column(percent(), nullable=False)

    # -------------------------------------------------------- daily limits ---
    daily_maximum_loss_percent: Mapped[Decimal] = mapped_column(percent(), nullable=False)
    #: R-26. Phase 0 decision OD-06 chose the conservative basis.
    daily_pnl_basis: Mapped[DailyPnlBasis] = mapped_column(
        enum_column(DailyPnlBasis, name="daily_pnl_basis"),
        nullable=False,
        default=DailyPnlBasis.REALISED_PLUS_UNREALISED,
    )
    #: R-23. NULL means the daily profit floor is disabled entirely, which is
    #: what Phase 0 decision OD-03 selected. Kept as a column so the protection
    #: can be switched on later without a schema migration.
    daily_profit_giveback_percent: Mapped[Decimal | None] = mapped_column(percent(), nullable=True)

    # --------------------------------------------------------- trade limits ---
    maximum_risk_per_trade_percent: Mapped[Decimal] = mapped_column(percent(), nullable=False)
    maximum_open_positions: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_trades_per_day: Mapped[int] = mapped_column(Integer, nullable=False)
    maximum_consecutive_losses: Mapped[int] = mapped_column(Integer, nullable=False)
    no_new_entry_minutes_before_day_end: Mapped[int] = mapped_column(Integer, nullable=False)

    # ------------------------------------------------- market quality gates ---
    # R-09 to R-15 and R-22. Nullable because Phase 0 deliberately left them
    # uncalibrated: they are set from real data in Phases 4 to 6, not guessed
    # now. A NULL means "not yet calibrated", and the risk engine treats an
    # uncalibrated mandatory gate as a refusal, never as an allowance.
    max_candle_age_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_signal_age_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_spread_bps: Mapped[Decimal | None] = mapped_column(percent(), nullable=True)
    min_order_book_depth_quote: Mapped[Decimal | None] = mapped_column(monetary(), nullable=True)
    min_atr_percent: Mapped[Decimal | None] = mapped_column(percent(), nullable=True)
    max_atr_percent: Mapped[Decimal | None] = mapped_column(percent(), nullable=True)
    min_reward_risk_ratio: Mapped[Decimal | None] = mapped_column(percent(), nullable=True)
    max_estimated_slippage_bps: Mapped[Decimal | None] = mapped_column(percent(), nullable=True)
    max_clock_drift_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -------------------------------------------------------------- audit ----
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="SYSTEM")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<RiskConfiguration v{self.version} active={self.is_active}>"


class TradingConfiguration(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """Operating mode and trading identity, versioned alongside risk rules."""

    __tablename__ = "trading_configuration"
    __table_args__ = (
        Index(
            "uq_trading_configuration_active",
            "is_active",
            unique=True,
            postgresql_where=text("is_active"),
        ),
        CheckConstraint("usdt_usd_peg > 0", name="usdt_usd_peg_positive"),
    )

    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: Server-side authority on the mode. The environment variable decides what
    #: the process is *allowed* to do; this row records what it is *set* to.
    autonomy_mode: Mapped[AutonomyMode] = mapped_column(
        enum_column(AutonomyMode, name="autonomy_mode"),
        nullable=False,
        default=AutonomyMode.SIGNAL_ONLY,
    )
    #: R-20. When true, every risk assessment is refused.
    emergency_stop_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    emergency_stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    reporting_currency: Mapped[str] = mapped_column(currency_code(), nullable=False, default="RON")
    exchange_quote_currency: Mapped[str] = mapped_column(
        currency_code(), nullable=False, default="USDT"
    )
    trading_timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="Europe/Bucharest"
    )
    primary_timeframe: Mapped[Timeframe] = mapped_column(
        enum_column(Timeframe, name="primary_timeframe"),
        nullable=False,
        default=Timeframe.M15,
    )

    # ------------------------------------------------------------------ fx ---
    fx_rate_source: Mapped[str] = mapped_column(String(32), nullable=False, default="BNR")
    #: A declared assumption, stored and auditable, never a hidden constant.
    #: BNR publishes RON/USD, not RON/USDT. USDT is pegged to USD but is not USD.
    usdt_usd_peg: Mapped[Decimal] = mapped_column(fx_rate(), nullable=False)

    #: R-21. NULL means 24/7, the Phase 0 starting point. Weak hours are excluded
    #: later on evidence collected by the system, not on guesswork now.
    trading_windows: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="SYSTEM")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<TradingConfiguration v{self.version} mode={self.autonomy_mode}>"
