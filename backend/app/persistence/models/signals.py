"""Signals and risk assessments.

Together with ``Strategy``/``StrategyVersion`` these form the decision chain
that makes "every trade must be reconstructable" achievable:

    StrategyVersion -> Signal -> RiskAssessment -> Order -> Fill -> Trade

Every row in the chain carries the same ``correlation_id``, so one query
returns the whole decision, in order, months later.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKey, Index, Numeric, String, Text, Uuid
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.domain.enums import OrderSide, RiskVerdict, SignalStatus, Timeframe
from app.persistence.base import Base
from app.persistence.mixins import CreatedAtMixin, TimestampMixin, UuidPrimaryKeyMixin
from app.persistence.models.strategy import StrategyVersion
from app.persistence.types import UtcDateTime, enum_column, monetary, price, quantity


class Signal(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A proposal produced by a strategy. Never an instruction to trade.

    A signal only ever reaches execution through a ``RiskAssessment``. The state
    machine enforces that: ``GENERATED`` can only move to ``RISK_APPROVED`` or
    ``RISK_REJECTED``.
    """

    __tablename__ = "signal"
    __table_args__ = (
        Index("ix_signal_status", "status"),
        Index("ix_signal_correlation_id", "correlation_id"),
        Index("ix_signal_pair_generated_at", "trading_pair_id", "generated_at"),
        CheckConstraint("reference_price > 0", name="reference_price_positive"),
        CheckConstraint("stop_loss_price > 0", name="stop_loss_price_positive"),
        CheckConstraint(
            "take_profit_price IS NULL OR take_profit_price > 0",
            name="take_profit_price_positive",
        ),
        # A long entry with the stop above the entry price is not a strategy
        # choice, it is an inverted sign. The database refuses it outright.
        CheckConstraint(
            "side <> 'BUY' OR stop_loss_price < reference_price",
            name="long_stop_below_entry",
        ),
        CheckConstraint(
            "side <> 'BUY' OR take_profit_price IS NULL OR take_profit_price > reference_price",
            name="long_target_above_entry",
        ),
    )

    trading_day_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_day.id"), nullable=False, index=True
    )
    #: NULL when the signal was generated while no session was open, which is
    #: the normal case: a session starts *because* a signal qualified.
    trading_session_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("trading_session.id"), nullable=True
    )
    trading_pair_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_pair.id"), nullable=False
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy_version.id"), nullable=False, index=True
    )

    status: Mapped[SignalStatus] = mapped_column(
        enum_column(SignalStatus, name="signal_status"),
        nullable=False,
        default=SignalStatus.GENERATED,
    )
    side: Mapped[OrderSide] = mapped_column(
        enum_column(OrderSide, name="order_side"), nullable=False
    )
    timeframe: Mapped[Timeframe] = mapped_column(
        enum_column(Timeframe, name="timeframe"), nullable=False
    )

    generated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    #: Rule R-10. A signal computed on a candle that has since been overtaken by
    #: the market is not a signal any more.
    expires_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    #: Open time of the candle that produced it. Populated from Phase 3.
    candle_open_time: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)

    # ------------------------------------------------------------- proposal ---
    reference_price: Mapped[Decimal] = mapped_column(price(), nullable=False)
    stop_loss_price: Mapped[Decimal] = mapped_column(price(), nullable=False)
    take_profit_price: Mapped[Decimal | None] = mapped_column(price(), nullable=True)
    proposed_quantity: Mapped[Decimal | None] = mapped_column(quantity(), nullable=True)
    risk_amount_quote: Mapped[Decimal | None] = mapped_column(monetary(), nullable=True)
    #: Net of estimated fees and slippage (rule R-14), not gross.
    reward_risk_ratio: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)

    # ---------------------------------------------------------------- score ---
    #: An internal RANKING score, not a probability. It must not be presented as
    #: one unless it has been calibrated and validated statistically, which it
    #: has not been.
    confidence_score: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    #: The individual components behind the score. Storing only the total would
    #: make a score impossible to audit or to improve.
    score_components: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    # ------------------------------------------------------------- evidence ---
    #: Indicator values the strategy actually saw. Without these the decision
    #: cannot be replayed, only guessed at.
    strategy_inputs: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    #: Spread, depth and data freshness at decision time.
    market_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    #: Ties this signal to every downstream row it causes.
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    # ------------------------------------------------- SIGNAL_ONLY operator ---
    operator_decision_at: Mapped[datetime | None] = mapped_column(UtcDateTime, nullable=True)
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    strategy_version: Mapped[StrategyVersion] = relationship()
    risk_assessments: Mapped[list[RiskAssessment]] = relationship(
        back_populates="signal",
        cascade="all, delete-orphan",
        order_by="RiskAssessment.evaluated_at",
    )

    @property
    def stop_distance(self) -> Decimal:
        """Absolute distance to the stop. The 1R denominator for sizing."""
        return abs(self.reference_price - self.stop_loss_price)

    def has_expired(self, at: datetime) -> bool:
        if self.expires_at is None:
            return False
        if at.tzinfo is None:
            raise ValueError("has_expired requires a timezone-aware datetime")
        return at >= self.expires_at

    def __repr__(self) -> str:
        return f"<Signal {self.side} {self.status} @{self.reference_price}>"


class RiskAssessment(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """The risk engine's verdict, with everything needed to explain it.

    Append-only. A verdict is a record of one moment; editing it would make the
    audit trail a work of fiction.
    """

    __tablename__ = "risk_assessment"
    __table_args__ = (
        Index("ix_risk_assessment_verdict", "verdict"),
        Index("ix_risk_assessment_evaluated_at", "evaluated_at"),
        Index("ix_risk_assessment_correlation_id", "correlation_id"),
        # GIN index over the reason codes, so "every refusal caused by stale
        # market data last month" is one indexed query rather than a scan.
        Index("ix_risk_assessment_reason_codes", "reason_codes", postgresql_using="gin"),
        # A refusal with no reason cannot be explained to an operator, and the
        # specification requires every rejection to be explainable. The database
        # refuses to store one.
        #
        # cardinality(), not array_length(). For an empty array array_length()
        # returns NULL, NULL >= 1 is NULL, and a CHECK that evaluates to NULL
        # PASSES. The constraint would have looked right and enforced nothing.
        # cardinality() returns 0 for an empty array, which compares properly.
        CheckConstraint(
            "verdict <> 'REJECTED' OR cardinality(reason_codes) >= 1",
            name="rejection_has_reason_codes",
        ),
    )

    #: NULL when the assessment is a day- or session-level gate rather than a
    #: verdict on a specific proposal, for example "may a session start now".
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("signal.id", ondelete="CASCADE"), nullable=True, index=True
    )
    trading_day_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("trading_day.id"), nullable=False, index=True
    )
    #: Which configuration produced this verdict. Without it the numbers behind
    #: the decision are unknowable after the next configuration change.
    risk_configuration_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("risk_configuration.id"), nullable=False
    )

    verdict: Mapped[RiskVerdict] = mapped_column(
        enum_column(RiskVerdict, name="risk_verdict"), nullable=False
    )
    #: Machine-readable reasons, from RiskReasonCode. Empty for an approval.
    reason_codes: Mapped[list[str]] = mapped_column(ARRAY(String(64)), nullable=False, default=list)

    #: Every rule evaluated, with its inputs, parameters and per-rule verdict.
    #: Storing only the final verdict would make an approval as unexplainable as
    #: a refusal.
    evaluated_rules: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    #: Human-readable explanation, for an operator who does not read code.
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ------------------------------------------------------ approved sizing ---
    approved_quantity: Mapped[Decimal | None] = mapped_column(quantity(), nullable=True)
    #: The risk after exchange-filter rounding, not before. Rounding changes it,
    #: which is why the engine recomputes and re-checks rather than trusting the
    #: raw figure.
    approved_risk_quote: Mapped[Decimal | None] = mapped_column(monetary(), nullable=True)

    evaluated_at: Mapped[datetime] = mapped_column(UtcDateTime, nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)

    signal: Mapped[Signal | None] = relationship(back_populates="risk_assessments")

    @property
    def is_approved(self) -> bool:
        return self.verdict is RiskVerdict.APPROVED

    def __repr__(self) -> str:
        return f"<RiskAssessment {self.verdict} {self.reason_codes}>"
