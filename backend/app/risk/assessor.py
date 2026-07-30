"""Evaluating a signal against the risk rules, and recording the verdict.

The one place a ``Signal`` becomes something execution may act on. Everything
here exists to make one property true: **an approved order can be traced back to
the exact rules, parameters and inputs that approved it, months later.**

Two decisions are worth stating up front.

**The day is judged by the configuration it was opened under**, read through
``TradingDay.risk_configuration_id`` and never through "whichever is active
now". Configuration is versioned precisely so a change at 14:00 cannot rewrite
what the rules were at 09:00, and reading the active row would throw that away.

**The assessment is written whatever the verdict.** A rejection is a record, not
an absence of one: "no order was placed" and "an order was refused because the
spread was 9 bps" are different facts, and only one of them can be learned from.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.clock import Clock, SystemClock
from app.core.logging import get_logger
from app.domain.enums import AuditActor, ExecutionVenue, RiskVerdict, SignalStatus
from app.domain.errors import DomainError
from app.domain.position_sizing import SizingResult
from app.domain.risk.context import (
    MarketState,
    ProposalUnderReview,
    RiskContext,
    SystemState,
)
from app.domain.risk.economics import TradingCosts, net_reward_risk_ratio
from app.domain.risk.engine import RiskDecision, evaluate
from app.domain.state_machines import SIGNAL_MACHINE
from app.persistence.models import (
    RiskAssessment,
    RiskConfiguration,
    Signal,
    TradingDay,
    TradingSession,
)
from app.persistence.repositories import (
    AuditRepository,
    ConfigurationRepository,
    PositionRepository,
    RiskAssessmentRepository,
    TradeRepository,
)
from app.persistence.state_transitions import apply_transition
from app.risk.mapping import DayStateSources, day_state_from_records, limits_from_configuration

logger = get_logger(__name__)


class RiskAssessorError(DomainError):
    """The assessment could not be performed at all."""


@dataclass(frozen=True, slots=True)
class AssessmentResult:
    """What was decided, and the row that records it."""

    decision: RiskDecision
    assessment: RiskAssessment

    @property
    def is_approved(self) -> bool:
        return self.decision.is_approved


class RiskAssessor:
    """Turns a signal plus the state of the world into a recorded verdict."""

    def __init__(self, session: AsyncSession, *, clock: Clock | None = None) -> None:
        self._session = session
        self._clock = clock if clock is not None else SystemClock()
        self._configurations = ConfigurationRepository(session)
        self._positions = PositionRepository(session)
        self._trades = TradeRepository(session)
        self._assessments = RiskAssessmentRepository(session)
        self._audit = AuditRepository(session)

    async def assess(
        self,
        *,
        signal: Signal,
        day: TradingDay,
        venue: ExecutionVenue,
        costs: TradingCosts,
        sizing: SizingResult | None = None,
        market: MarketState | None = None,
        system: SystemState | None = None,
        session: TradingSession | None = None,
        time_remaining_in_day: timedelta | None = None,
        unrealised_pnl_quote: Decimal | None = None,
    ) -> AssessmentResult:
        """Evaluate one signal and persist the verdict."""
        now = self._clock.now()
        limits = limits_from_configuration(await self._configuration_for(day))

        sources = DayStateSources(
            open_positions=await self._positions.count_occupying_slots(venue),
            consecutive_losses=await self._trades.count_consecutive_losses(venue),
            time_remaining_in_day=time_remaining_in_day,
            unrealised_pnl_quote=unrealised_pnl_quote,
        )
        context = RiskContext(
            evaluated_at=now,
            limits=limits,
            day=day_state_from_records(day, sources, session),
            market=market if market is not None else MarketState(),
            system=self._system_state(system, sizing),
            proposal=self._proposal(signal, sizing, costs, now),
        )

        decision = evaluate(context)
        assessment = await self._record(signal, day, decision, sizing, now)
        await self._transition(signal, decision)

        logger.info(
            "risk_assessment_completed",
            signal_id=str(signal.id),
            correlation_id=str(signal.correlation_id),
            verdict=decision.verdict.value,
            action=decision.action.value if decision.action else None,
            reason_codes=[code.value for code in decision.reason_codes],
            uncalibrated_rules=list(decision.uncalibrated_rules),
        )
        return AssessmentResult(decision=decision, assessment=assessment)

    # ------------------------------------------------------------ internals ---

    async def _configuration_for(self, day: TradingDay) -> RiskConfiguration:
        """The configuration the day was OPENED under, never today's active one.

        Configuration is versioned so a change at 14:00 cannot rewrite what the
        rules were at 09:00. Reading the active row would discard exactly that.
        """
        configuration = await self._configurations.get_risk_configuration_by_id(
            day.risk_configuration_id
        )
        if configuration is None:
            raise RiskAssessorError(
                f"Trading day {day.trading_date} references risk configuration "
                f"{day.risk_configuration_id}, which no longer exists."
            )
        return configuration

    def _system_state(
        self, provided: SystemState | None, sizing: SizingResult | None
    ) -> SystemState:
        """Fold sizing's conclusion into the system state.

        Sizing already applied the exchange filters; R-16 and R-17 report that
        conclusion rather than recomputing it, because a second implementation
        of the filter arithmetic can disagree with the first.
        """
        base = provided if provided is not None else SystemState()
        if sizing is None:
            # Never treated as viable. A proposal that was never sized cannot be
            # ordered, and assuming otherwise would be assuming the safest case.
            return SystemState(
                health=base.health,
                exchange_healthy=base.exchange_healthy,
                emergency_stop_active=base.emergency_stop_active,
                strategy_enabled=base.strategy_enabled,
                within_trading_window=base.within_trading_window,
                sizing_is_viable=False,
                sizing_reason_codes=(),
            )
        return SystemState(
            health=base.health,
            exchange_healthy=base.exchange_healthy,
            emergency_stop_active=base.emergency_stop_active,
            strategy_enabled=base.strategy_enabled,
            within_trading_window=base.within_trading_window,
            sizing_is_viable=sizing.is_viable,
            sizing_reason_codes=tuple(code.value for code in sizing.reason_codes),
        )

    def _proposal(
        self,
        signal: Signal,
        sizing: SizingResult | None,
        costs: TradingCosts,
        now: datetime,
    ) -> ProposalUnderReview:
        """Reduce the stored signal to what the rules judge.

        The reward-to-risk ratio is recomputed here NET of costs. The signal
        carries a gross figure, and R-14 is explicit that the gross number is
        not what it checks.
        """
        entry = (
            sizing.entry_price
            if sizing is not None and sizing.is_viable
            else (signal.reference_price)
        )
        stop = (
            sizing.stop_loss_price
            if sizing is not None and sizing.is_viable
            else (signal.stop_loss_price)
        )
        target = (
            sizing.take_profit_price
            if sizing is not None and sizing.is_viable
            else signal.take_profit_price
        )

        net_ratio: Decimal | None = None
        if target is not None and entry > 0 and entry != stop:
            net_ratio = net_reward_risk_ratio(
                entry_price=entry,
                stop_loss_price=stop,
                take_profit_price=target,
                costs=costs,
            )

        return ProposalUnderReview(
            side=signal.side,
            entry_price=entry,
            stop_loss_price=stop,
            take_profit_price=target,
            net_reward_risk_ratio=net_ratio,
            quantity=sizing.quantity if sizing is not None and sizing.is_viable else None,
            risk_amount_reporting=(
                sizing.risk_reporting if sizing is not None and sizing.is_viable else None
            ),
            notional_quote=(
                sizing.notional_quote if sizing is not None and sizing.is_viable else None
            ),
            signal_age=now - signal.generated_at,
        )

    async def _record(
        self,
        signal: Signal,
        day: TradingDay,
        decision: RiskDecision,
        sizing: SizingResult | None,
        now: datetime,
    ) -> RiskAssessment:
        approved = decision.is_approved and sizing is not None and sizing.is_viable
        return await self._assessments.add(
            RiskAssessment(
                signal_id=signal.id,
                trading_day_id=day.id,
                risk_configuration_id=day.risk_configuration_id,
                verdict=decision.verdict,
                reason_codes=[code.value for code in decision.reason_codes],
                evaluated_rules=decision.as_evaluated_rules(),
                explanation=decision.explanation,
                # Written only on an approval. A quantity stored beside a
                # rejection is a number somebody will eventually act on.
                approved_quantity=sizing.quantity if approved and sizing else None,
                approved_risk_quote=sizing.risk_quote if approved and sizing else None,
                evaluated_at=now,
                correlation_id=signal.correlation_id,
            )
        )

    async def _transition(self, signal: Signal, decision: RiskDecision) -> None:
        target = SignalStatus.RISK_APPROVED if decision.is_approved else SignalStatus.RISK_REJECTED
        await apply_transition(
            entity=signal,
            machine=SIGNAL_MACHINE,
            target=target,
            audit=self._audit,
            aggregate_type="signal",
            event_type="signal.risk_assessed",
            actor=AuditActor.RISK_ENGINE,
            reason=", ".join(code.value for code in decision.reason_codes) or None,
            summary=decision.explanation.splitlines()[0],
            correlation_id=signal.correlation_id,
            payload={
                "verdict": decision.verdict.value,
                "action": decision.action.value if decision.action else None,
                "uncalibrated_rules": list(decision.uncalibrated_rules),
            },
        )


def approved_assessment_id(result: AssessmentResult) -> uuid.UUID | None:
    """The id an ENTRY order must carry, or ``None`` if there is none.

    The database refuses an ENTRY order whose ``risk_assessment_id`` is NULL, so
    this returning ``None`` is what stops an unapproved trade rather than a
    convention anyone has to remember.
    """
    if result.decision.verdict is not RiskVerdict.APPROVED:
        return None
    return result.assessment.id
