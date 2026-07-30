"""The risk rules, as data.

Each rule is a descriptor - identifier, name, reason code, consequence - paired
with a pure function. Not a chain of conditionals inside one large method,
because a chain has no list: nothing can enumerate it, nothing can assert that
every documented rule exists, and a rule quietly deleted during a refactor
leaves no trace. A table can be walked, counted and checked against the
specification, and a test does exactly that.

Every rule returns the inputs it read and the parameters it compared them
against, whatever the outcome. Section 3 of docs/RISK_RULES.md requires it, and
the reason is simple: an approval nobody can explain is as useless as a refusal
nobody can explain. "It passed" is not an explanation; "risk 4.83 RON against a
5.00 RON budget" is.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from app.domain.enums import (
    HealthStatus,
    RiskReasonCode,
    RuleAction,
    RuleStatus,
)
from app.domain.risk.context import RiskContext

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class RuleOutcome:
    """What one rule concluded, and everything needed to explain it."""

    rule_id: str
    name: str
    status: RuleStatus
    action: RuleAction
    reason_code: RiskReasonCode | None = None
    inputs: dict[str, str] = field(default_factory=dict)
    parameters: dict[str, str] = field(default_factory=dict)
    detail: str = ""

    @property
    def blocks(self) -> bool:
        """Whether this outcome prevents the proposal under review.

        Two conditions, not one. ``ALLOW_RE_EVALUATION`` triggering is a
        permission - R-08 firing means a new session MAY start - so treating
        every triggered rule as a refusal would invert it.
        """
        return self.status.blocks and self.action.refuses_the_proposal

    def as_record(self) -> dict[str, object]:
        """The shape persisted in ``RiskAssessment.evaluated_rules``."""
        return {
            "rule_id": self.rule_id,
            "name": self.name,
            "status": self.status.value,
            "action": self.action.value,
            "reason_code": self.reason_code.value if self.reason_code else None,
            "inputs": self.inputs,
            "parameters": self.parameters,
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class RiskRule:
    """One rule from docs/RISK_RULES.md, bound to the code that evaluates it."""

    rule_id: str
    name: str
    reason_code: RiskReasonCode
    action: RuleAction
    evaluate: Callable[[RiskContext], RuleOutcome]

    def __call__(self, context: RiskContext) -> RuleOutcome:
        return self.evaluate(context)


# --------------------------------------------------------------- builders ---
#
# The four outcomes a rule can reach. Kept as helpers so a rule body reads as
# its own logic rather than as dataclass construction.


def _passed(
    rule: _Spec,
    *,
    inputs: dict[str, str] | None = None,
    parameters: dict[str, str] | None = None,
    detail: str = "",
) -> RuleOutcome:
    return _build(
        rule,
        RuleStatus.PASSED,
        reason_code=None,
        inputs=inputs,
        parameters=parameters,
        detail=detail,
    )


def _triggered(
    rule: _Spec,
    *,
    inputs: dict[str, str] | None = None,
    parameters: dict[str, str] | None = None,
    detail: str = "",
) -> RuleOutcome:
    return _build(
        rule,
        RuleStatus.TRIGGERED,
        reason_code=rule.reason_code,
        inputs=inputs,
        parameters=parameters,
        detail=detail,
    )


def _not_applicable(
    rule: _Spec,
    detail: str,
    *,
    inputs: dict[str, str] | None = None,
    parameters: dict[str, str] | None = None,
) -> RuleOutcome:
    return _build(
        rule,
        RuleStatus.NOT_APPLICABLE,
        reason_code=None,
        inputs=inputs,
        parameters=parameters,
        detail=detail,
    )


def _not_calibrated(rule: _Spec, parameter: str) -> RuleOutcome:
    """A mandatory threshold that was never measured.

    Reported under RISK_CONFIGURATION_INCOMPLETE rather than the rule's own
    code. Saying SPREAD_TOO_WIDE when nobody ever set a maximum spread would
    tell an operator the market was bad, when the truth is that the
    configuration is missing.
    """
    return _build(
        rule,
        RuleStatus.NOT_CALIBRATED,
        reason_code=RiskReasonCode.RISK_CONFIGURATION_INCOMPLETE,
        parameters={parameter: "not calibrated"},
        detail=(
            f"{parameter} has never been calibrated, so this gate cannot be judged. "
            f"An unknown limit is not the absence of one."
        ),
    )


@dataclass(frozen=True, slots=True)
class _Spec:
    """The identity half of a rule, available to its own body."""

    rule_id: str
    name: str
    reason_code: RiskReasonCode
    action: RuleAction


def _build(
    rule: _Spec,
    status: RuleStatus,
    *,
    reason_code: RiskReasonCode | None,
    inputs: dict[str, str] | None = None,
    parameters: dict[str, str] | None = None,
    detail: str = "",
) -> RuleOutcome:
    return RuleOutcome(
        rule_id=rule.rule_id,
        name=rule.name,
        status=status,
        action=rule.action,
        reason_code=reason_code,
        inputs=inputs or {},
        parameters=parameters or {},
        detail=detail,
    )


def _seconds(value: timedelta) -> str:
    return f"{value.total_seconds():.3f}"


# ------------------------------------------------------------------ rules ---

R02 = _Spec(
    "R-02",
    "Maximum risk per trade",
    RiskReasonCode.RISK_PER_TRADE_EXCEEDED,
    RuleAction.REJECT_ORDER,
)


def _r02(context: RiskContext) -> RuleOutcome:
    budget = context.limits.risk_per_trade_amount
    parameters = {"maximum_risk_per_trade": str(budget)}
    proposal = context.proposal
    if proposal is None:
        return _not_applicable(R02, "no proposal under review", parameters=parameters)
    if proposal.risk_amount_reporting is None:
        # Sizing has not run. Not knowing the risk is a reason to refuse, never
        # a reason to assume it is small.
        return _triggered(
            R02,
            inputs={"risk_amount": "unknown"},
            parameters=parameters,
            detail="Position size was never computed, so the risk is unknown.",
        )
    risk = proposal.risk_amount_reporting
    inputs = {"risk_amount": str(risk)}
    if risk > budget:
        return _triggered(
            R02,
            inputs=inputs,
            parameters=parameters,
            detail=f"Risk {risk} exceeds the per-trade budget {budget}.",
        )
    return _passed(
        R02,
        inputs=inputs,
        parameters=parameters,
        detail=f"Risk {risk} is within the per-trade budget {budget}.",
    )


R03 = _Spec(
    "R-03", "Maximum daily loss", RiskReasonCode.DAILY_LOSS_LIMIT_REACHED, RuleAction.HALT_DAY
)


def _r03(context: RiskContext) -> RuleOutcome:
    limits = context.limits
    day = context.day
    limit = limits.daily_maximum_loss_amount
    # R-26 selects the basis. Decision OD-06 chose the conservative one: an open
    # position sitting at -41 RON stops the day now, without waiting for it to
    # close and make the loss official.
    basis = limits.daily_pnl_basis
    pnl = day.total_pnl if basis.value == "REALISED_PLUS_UNREALISED" else day.realised_pnl
    inputs = {
        "realised_pnl": str(day.realised_pnl),
        "unrealised_pnl": str(day.unrealised_pnl),
        "evaluated_pnl": str(pnl),
    }
    parameters = {"daily_maximum_loss": str(limit), "daily_pnl_basis": basis.value}
    if pnl <= -limit:
        return _triggered(
            R03,
            inputs=inputs,
            parameters=parameters,
            detail=f"Day P&L {pnl} has reached the {-limit} floor on a {basis.value} basis.",
        )
    return _passed(
        R03,
        inputs=inputs,
        parameters=parameters,
        detail=f"Day P&L {pnl} is above the {-limit} floor.",
    )


R04 = _Spec(
    "R-04",
    "Simultaneous positions",
    RiskReasonCode.MAX_OPEN_POSITIONS_REACHED,
    RuleAction.REJECT_ORDER,
)


def _r04(context: RiskContext) -> RuleOutcome:
    allowed = context.limits.maximum_open_positions
    current = context.day.open_positions
    inputs = {"open_positions": str(current)}
    parameters = {"maximum_open_positions": str(allowed)}
    if current >= allowed:
        return _triggered(
            R04,
            inputs=inputs,
            parameters=parameters,
            detail=f"{current} position(s) already open, limit {allowed}.",
        )
    return _passed(R04, inputs=inputs, parameters=parameters)


R05 = _Spec(
    "R-05", "Trades per day", RiskReasonCode.MAX_TRADES_PER_DAY_REACHED, RuleAction.HALT_DAY
)


def _r05(context: RiskContext) -> RuleOutcome:
    allowed = context.limits.maximum_trades_per_day
    current = context.day.trades_today
    inputs = {"trades_today": str(current)}
    parameters = {"maximum_trades_per_day": str(allowed)}
    if current >= allowed:
        return _triggered(
            R05,
            inputs=inputs,
            parameters=parameters,
            detail=f"{current} trades today, limit {allowed}.",
        )
    return _passed(R05, inputs=inputs, parameters=parameters)


R06 = _Spec(
    "R-06",
    "Consecutive losses",
    RiskReasonCode.MAX_CONSECUTIVE_LOSSES_REACHED,
    RuleAction.HALT_DAY,
)


def _r06(context: RiskContext) -> RuleOutcome:
    allowed = context.limits.maximum_consecutive_losses
    current = context.day.consecutive_losses
    inputs = {"consecutive_losses": str(current)}
    parameters = {"maximum_consecutive_losses": str(allowed)}
    if current >= allowed:
        return _triggered(
            R06,
            inputs=inputs,
            parameters=parameters,
            detail=f"{current} consecutive losses, limit {allowed}.",
        )
    return _passed(R06, inputs=inputs, parameters=parameters)


R07 = _Spec(
    "R-07", "Session target", RiskReasonCode.SESSION_TARGET_REACHED, RuleAction.CLOSE_SESSION
)


def _r07(context: RiskContext) -> RuleOutcome:
    target = context.limits.session_target_amount
    pnl = context.day.session_pnl
    inputs = {"session_pnl": str(pnl)}
    parameters = {"session_target": str(target)}
    if pnl >= target:
        return _triggered(
            R07,
            inputs=inputs,
            parameters=parameters,
            detail=f"Session P&L {pnl} has reached the {target} target.",
        )
    return _passed(R07, inputs=inputs, parameters=parameters)


R08 = _Spec(
    "R-08",
    "Session restart threshold",
    RiskReasonCode.SESSION_RESTART_ELIGIBLE,
    RuleAction.ALLOW_RE_EVALUATION,
)


def _r08(context: RiskContext) -> RuleOutcome:
    """The one rule whose triggering is permission rather than refusal.

    Strictly above the threshold, per SRS 6.5: at exactly 4% the day stops. And
    eligibility never causes a session - a new one starts only if a fresh
    opportunity independently satisfies every criterion.
    """
    threshold = context.limits.session_restart_threshold_amount
    pnl = context.day.session_pnl
    inputs = {"session_pnl": str(pnl)}
    parameters = {"session_restart_threshold": str(threshold)}
    if pnl > threshold:
        return _triggered(
            R08,
            inputs=inputs,
            parameters=parameters,
            detail=(
                f"Session P&L {pnl} exceeds {threshold}; a new session becomes "
                f"possible, but only on an independently qualifying opportunity."
            ),
        )
    return _passed(R08, inputs=inputs, parameters=parameters)


R09 = _Spec(
    "R-09", "Market data freshness", RiskReasonCode.STALE_MARKET_DATA, RuleAction.REJECT_ORDER
)


def _r09(context: RiskContext) -> RuleOutcome:
    allowed = context.limits.max_candle_age_seconds
    if allowed is None:
        return _not_calibrated(R09, "max_candle_age_seconds")
    age = context.market.candle_age
    parameters = {"max_candle_age_seconds": str(allowed)}
    if age is None:
        # Not measured is not "fine". A strategy cannot tell the difference
        # between a quiet market and a dead feed; this rule is where that
        # difference is enforced.
        return _triggered(
            R09,
            inputs={"candle_age_seconds": "unknown"},
            parameters=parameters,
            detail="Candle age was not measured, so freshness cannot be established.",
        )
    inputs = {"candle_age_seconds": _seconds(age)}
    if age.total_seconds() > allowed:
        return _triggered(
            R09,
            inputs=inputs,
            parameters=parameters,
            detail=f"Newest candle is {_seconds(age)}s old, limit {allowed}s.",
        )
    return _passed(R09, inputs=inputs, parameters=parameters)


R10 = _Spec("R-10", "Signal age", RiskReasonCode.SIGNAL_EXPIRED, RuleAction.REJECT_ORDER)


def _r10(context: RiskContext) -> RuleOutcome:
    allowed = context.limits.max_signal_age_seconds
    if allowed is None:
        return _not_calibrated(R10, "max_signal_age_seconds")
    proposal = context.proposal
    parameters = {"max_signal_age_seconds": str(allowed)}
    if proposal is None:
        return _not_applicable(R10, "no proposal under review", parameters=parameters)
    age = proposal.signal_age
    if age is None:
        return _triggered(
            R10,
            inputs={"signal_age_seconds": "unknown"},
            parameters=parameters,
            detail="Signal age was not measured.",
        )
    inputs = {"signal_age_seconds": _seconds(age)}
    if age.total_seconds() > allowed:
        return _triggered(
            R10,
            inputs=inputs,
            parameters=parameters,
            detail=f"Signal is {_seconds(age)}s old, limit {allowed}s.",
        )
    return _passed(R10, inputs=inputs, parameters=parameters)


R11 = _Spec("R-11", "Maximum spread", RiskReasonCode.SPREAD_TOO_WIDE, RuleAction.REJECT_ORDER)


def _r11(context: RiskContext) -> RuleOutcome:
    allowed = context.limits.max_spread_bps
    if allowed is None:
        return _not_calibrated(R11, "max_spread_bps")
    observed = context.market.spread_bps
    parameters = {"max_spread_bps": str(allowed)}
    if observed is None:
        return _triggered(
            R11,
            inputs={"spread_bps": "unknown"},
            parameters=parameters,
            detail="Spread was not measured.",
        )
    inputs = {"spread_bps": str(observed)}
    if observed > allowed:
        return _triggered(
            R11,
            inputs=inputs,
            parameters=parameters,
            detail=f"Spread {observed} bps exceeds {allowed} bps.",
        )
    return _passed(R11, inputs=inputs, parameters=parameters)


R12 = _Spec("R-12", "Minimum liquidity", RiskReasonCode.LIQUIDITY_TOO_LOW, RuleAction.REJECT_ORDER)


def _r12(context: RiskContext) -> RuleOutcome:
    required = context.limits.min_order_book_depth_quote
    if required is None:
        return _not_calibrated(R12, "min_order_book_depth_quote")
    observed = context.market.order_book_depth_quote
    parameters = {"min_order_book_depth_quote": str(required)}
    if observed is None:
        return _triggered(
            R12,
            inputs={"order_book_depth_quote": "unknown"},
            parameters=parameters,
            detail="Order book depth was not measured.",
        )
    inputs = {"order_book_depth_quote": str(observed)}
    if observed < required:
        return _triggered(
            R12,
            inputs=inputs,
            parameters=parameters,
            detail=f"Depth {observed} is below the {required} minimum.",
        )
    return _passed(R12, inputs=inputs, parameters=parameters)


R13 = _Spec(
    "R-13", "Volatility band", RiskReasonCode.VOLATILITY_OUT_OF_RANGE, RuleAction.REJECT_ORDER
)


def _r13(context: RiskContext) -> RuleOutcome:
    low = context.limits.min_atr_percent
    high = context.limits.max_atr_percent
    if low is None or high is None:
        return _not_calibrated(R13, "min_atr_percent / max_atr_percent")
    observed = context.market.atr_percent
    parameters = {"min_atr_percent": str(low), "max_atr_percent": str(high)}
    if observed is None:
        return _triggered(
            R13,
            inputs={"atr_percent": "unknown"},
            parameters=parameters,
            detail="Volatility was not measured.",
        )
    inputs = {"atr_percent": str(observed)}
    if not (low <= observed <= high):
        return _triggered(
            R13,
            inputs=inputs,
            parameters=parameters,
            detail=f"ATR {observed}% is outside the [{low}, {high}] band.",
        )
    return _passed(R13, inputs=inputs, parameters=parameters)


R14 = _Spec(
    "R-14", "Minimum reward-to-risk", RiskReasonCode.REWARD_RISK_TOO_LOW, RuleAction.REJECT_ORDER
)


def _r14(context: RiskContext) -> RuleOutcome:
    required = context.limits.min_reward_risk_ratio
    if required is None:
        return _not_calibrated(R14, "min_reward_risk_ratio")
    proposal = context.proposal
    parameters = {"min_reward_risk_ratio": str(required)}
    if proposal is None:
        return _not_applicable(R14, "no proposal under review", parameters=parameters)
    observed = proposal.net_reward_risk_ratio
    if observed is None:
        # The gross ratio is not accepted as a substitute anywhere. It is the
        # number that flatters, and this rule is about the one that does not.
        return _triggered(
            R14,
            inputs={"net_reward_risk_ratio": "unknown"},
            parameters=parameters,
            detail="Net reward-to-risk was never computed; the gross ratio is not a substitute.",
        )
    inputs = {"net_reward_risk_ratio": str(observed)}
    if observed < required:
        return _triggered(
            R14,
            inputs=inputs,
            parameters=parameters,
            detail=f"Net reward-to-risk {observed} is below the {required} minimum.",
        )
    return _passed(R14, inputs=inputs, parameters=parameters)


R15 = _Spec(
    "R-15",
    "Maximum estimated slippage",
    RiskReasonCode.ESTIMATED_SLIPPAGE_TOO_HIGH,
    RuleAction.REJECT_ORDER,
)


def _r15(context: RiskContext) -> RuleOutcome:
    allowed = context.limits.max_estimated_slippage_bps
    if allowed is None:
        return _not_calibrated(R15, "max_estimated_slippage_bps")
    observed = context.market.estimated_slippage_bps
    parameters = {"max_estimated_slippage_bps": str(allowed)}
    if observed is None:
        return _triggered(
            R15,
            inputs={"estimated_slippage_bps": "unknown"},
            parameters=parameters,
            detail="Slippage was not estimated.",
        )
    inputs = {"estimated_slippage_bps": str(observed)}
    if observed > allowed:
        return _triggered(
            R15,
            inputs=inputs,
            parameters=parameters,
            detail=f"Estimated slippage {observed} bps exceeds {allowed} bps.",
        )
    return _passed(R15, inputs=inputs, parameters=parameters)


R16 = _Spec(
    "R-16", "Exchange filters", RiskReasonCode.EXCHANGE_FILTER_VIOLATION, RuleAction.REJECT_ORDER
)


def _r16(context: RiskContext) -> RuleOutcome:
    """Reports what position sizing already concluded.

    The filter arithmetic lives in ``app.domain.position_sizing``; repeating it
    here would create a second implementation that can disagree with the first.
    This rule carries that conclusion into the audit record.
    """
    system = context.system
    inputs = {
        "sizing_is_viable": str(system.sizing_is_viable),
        "sizing_reason_codes": ",".join(system.sizing_reason_codes) or "none",
    }
    if not system.sizing_is_viable:
        # The SPECIFIC code, when sizing gave one. The rule table lists both
        # EXCHANGE_FILTER_VIOLATION and MIN_NOTIONAL_NOT_MET against R-16, and
        # the difference is what an operator acts on: a minimum-notional refusal
        # says the risk budget is too small for this symbol and has an obvious
        # remedy, while a generic filter violation says something else entirely.
        # Collapsing them would hide the one that can be fixed.
        code = (
            RiskReasonCode.MIN_NOTIONAL_NOT_MET
            if RiskReasonCode.MIN_NOTIONAL_NOT_MET.value in system.sizing_reason_codes
            else RiskReasonCode.EXCHANGE_FILTER_VIOLATION
        )
        return _build(
            R16,
            RuleStatus.TRIGGERED,
            reason_code=code,
            inputs=inputs,
            detail=(
                "Position sizing could not produce an order the exchange would "
                f"accept: {', '.join(system.sizing_reason_codes) or 'no reason recorded'}."
            ),
        )
    return _passed(R16, inputs=inputs)


R17 = _Spec(
    "R-17", "Available balance", RiskReasonCode.INSUFFICIENT_BALANCE, RuleAction.REJECT_ORDER
)


def _r17(context: RiskContext) -> RuleOutcome:
    codes = context.system.sizing_reason_codes
    inputs = {"sizing_reason_codes": ",".join(codes) or "none"}
    if RiskReasonCode.INSUFFICIENT_BALANCE.value in codes:
        return _triggered(
            R17, inputs=inputs, detail="The position cannot be paid for from the quote balance."
        )
    return _passed(R17, inputs=inputs)


R18 = _Spec("R-18", "System health", RiskReasonCode.SYSTEM_HEALTH_DEGRADED, RuleAction.REJECT_ORDER)


def _r18(context: RiskContext) -> RuleOutcome:
    health = context.system.health
    inputs = {"system_health": health.value}
    parameters = {"required": HealthStatus.HEALTHY.value}
    if health.blocks_new_positions:
        return _triggered(
            R18,
            inputs=inputs,
            parameters=parameters,
            detail=(
                f"System health is {health.value}. Existing positions are still "
                f"managed; only new entries are refused."
            ),
        )
    return _passed(R18, inputs=inputs, parameters=parameters)


R19 = _Spec("R-19", "Exchange health", RiskReasonCode.EXCHANGE_UNHEALTHY, RuleAction.REJECT_ORDER)


def _r19(context: RiskContext) -> RuleOutcome:
    healthy = context.system.exchange_healthy
    inputs = {"exchange_healthy": str(healthy)}
    if not healthy:
        return _triggered(R19, inputs=inputs, detail="The exchange is not answering normally.")
    return _passed(R19, inputs=inputs)


R20 = _Spec("R-20", "Emergency stop", RiskReasonCode.EMERGENCY_STOP_ACTIVE, RuleAction.REJECT_ALL)


def _r20(context: RiskContext) -> RuleOutcome:
    active = context.system.emergency_stop_active
    inputs = {"emergency_stop_active": str(active)}
    if active:
        return _triggered(
            R20,
            inputs=inputs,
            detail="The emergency stop is engaged. Only an operator can clear it.",
        )
    return _passed(R20, inputs=inputs)


R21 = _Spec("R-21", "Trading window", RiskReasonCode.TRADING_WINDOW_CLOSED, RuleAction.REJECT_ORDER)


def _r21(context: RiskContext) -> RuleOutcome:
    inside = context.system.within_trading_window
    inputs = {"within_trading_window": str(inside)}
    if not inside:
        return _triggered(R21, inputs=inputs, detail="Outside the configured trading window.")
    return _passed(R21, inputs=inputs)


R22 = _Spec("R-22", "Clock drift", RiskReasonCode.CLOCK_DRIFT_EXCEEDED, RuleAction.REJECT_ORDER)


def _r22(context: RiskContext) -> RuleOutcome:
    allowed = context.limits.max_clock_drift_ms
    if allowed is None:
        return _not_calibrated(R22, "max_clock_drift_ms")
    observed = context.market.clock_drift_ms
    parameters = {"max_clock_drift_ms": str(allowed)}
    if observed is None:
        return _triggered(
            R22,
            inputs={"clock_drift_ms": "unknown"},
            parameters=parameters,
            detail="Clock drift was not measured.",
        )
    inputs = {"clock_drift_ms": str(observed)}
    # Absolute: a clock that is behind is as dangerous as one that is ahead,
    # and a signed comparison would silently accept half of the problem.
    if abs(observed) > allowed:
        return _triggered(
            R22,
            inputs=inputs,
            parameters=parameters,
            detail=f"Clock drift {observed} ms exceeds {allowed} ms.",
        )
    return _passed(R22, inputs=inputs, parameters=parameters)


R23 = _Spec(
    "R-23", "Daily profit floor", RiskReasonCode.DAILY_PROFIT_FLOOR_REACHED, RuleAction.HALT_DAY
)


def _r23(context: RiskContext) -> RuleOutcome:
    """Disabled by decision OD-03, and reported as disabled rather than passed.

    The distinction is the point: NOT_APPLICABLE says a decision switched this
    off, PASSED would say it was checked and the day was fine. A later reader
    must be able to tell that a day gave back 85 RON with no protection because
    that was chosen.
    """
    giveback = context.limits.profit_giveback_amount(context.day.peak_pnl)
    if context.limits.daily_profit_giveback_percent is None:
        return _not_applicable(
            R23,
            "Profit protection is switched off by decision OD-03; the only floor "
            "is the daily maximum loss.",
            inputs={"peak_pnl": str(context.day.peak_pnl)},
            parameters={"daily_profit_giveback_percent": "disabled"},
        )
    if giveback is None:
        return _not_applicable(
            R23,
            "No profit has been made today, so there is none to protect.",
            inputs={"peak_pnl": str(context.day.peak_pnl)},
        )
    floor = context.day.peak_pnl - giveback
    inputs = {"peak_pnl": str(context.day.peak_pnl), "current_pnl": str(context.day.total_pnl)}
    parameters = {
        "daily_profit_giveback_percent": str(context.limits.daily_profit_giveback_percent),
        "floor": str(floor),
    }
    if context.day.total_pnl <= floor:
        return _triggered(
            R23,
            inputs=inputs,
            parameters=parameters,
            detail=f"P&L {context.day.total_pnl} has fallen to the {floor} profit floor.",
        )
    return _passed(R23, inputs=inputs, parameters=parameters)


R24 = _Spec(
    "R-24",
    "Day-boundary entry block",
    RiskReasonCode.DAY_BOUNDARY_NO_ENTRY_WINDOW,
    RuleAction.REJECT_ORDER,
)


def _r24(context: RiskContext) -> RuleOutcome:
    minutes = context.limits.no_new_entry_minutes_before_day_end
    remaining = context.day.time_remaining_in_day
    parameters = {"no_new_entry_minutes_before_day_end": str(minutes)}
    if minutes == 0:
        return _not_applicable(
            R24, "The day-boundary block is switched off.", parameters=parameters
        )
    if remaining is None:
        return _triggered(
            R24,
            inputs={"time_remaining_seconds": "unknown"},
            parameters=parameters,
            detail="Time remaining in the trading day is unknown.",
        )
    inputs = {"time_remaining_seconds": _seconds(remaining)}
    if remaining <= timedelta(minutes=minutes):
        return _triggered(
            R24,
            inputs=inputs,
            parameters=parameters,
            detail=(
                f"Only {_seconds(remaining)}s remain in the trading day; entries "
                f"stop {minutes} minutes before it ends."
            ),
        )
    return _passed(R24, inputs=inputs, parameters=parameters)


R27 = _Spec("R-27", "Strategy enabled", RiskReasonCode.STRATEGY_DISABLED, RuleAction.REJECT_ORDER)


def _r27(context: RiskContext) -> RuleOutcome:
    enabled = context.system.strategy_enabled
    inputs = {"strategy_enabled": str(enabled)}
    if not enabled:
        return _triggered(
            R27,
            inputs=inputs,
            detail="The strategy that produced this proposal is not enabled.",
        )
    return _passed(R27, inputs=inputs)


def _rule(spec: _Spec, evaluate: Callable[[RiskContext], RuleOutcome]) -> RiskRule:
    return RiskRule(
        rule_id=spec.rule_id,
        name=spec.name,
        reason_code=spec.reason_code,
        action=spec.action,
        evaluate=evaluate,
    )


#: Every rule, in identifier order. The engine walks this and nothing else, so a
#: rule that is not here does not run - which is why a test checks the table
#: against docs/RISK_RULES.md rather than trusting that it is complete.
ALL_RULES: tuple[RiskRule, ...] = (
    _rule(R02, _r02),
    _rule(R03, _r03),
    _rule(R04, _r04),
    _rule(R05, _r05),
    _rule(R06, _r06),
    _rule(R07, _r07),
    _rule(R08, _r08),
    _rule(R09, _r09),
    _rule(R10, _r10),
    _rule(R11, _r11),
    _rule(R12, _r12),
    _rule(R13, _r13),
    _rule(R14, _r14),
    _rule(R15, _r15),
    _rule(R16, _r16),
    _rule(R17, _r17),
    _rule(R18, _r18),
    _rule(R19, _r19),
    _rule(R20, _r20),
    _rule(R21, _r21),
    _rule(R22, _r22),
    _rule(R23, _r23),
    _rule(R24, _r24),
    _rule(R27, _r27),
)

#: Reason codes that no rule emits, with the reason recorded rather than left to
#: be discovered. A test asserts this list plus the rules' codes covers the whole
#: enum, so a code that becomes orphaned by a refactor fails the build.
CODES_EMITTED_ELSEWHERE: dict[RiskReasonCode, str] = {
    RiskReasonCode.MIN_NOTIONAL_NOT_MET: (
        "Emitted by R-16 in place of its own code when position sizing named "
        "this specific reason, so the refusal an operator can act on is not "
        "collapsed into the generic one."
    ),
    RiskReasonCode.NO_VALID_OPPORTUNITY: (
        "Emitted by the signal engine when no strategy proposed anything. There "
        "is no proposal for the risk engine to judge, so it is not a rule."
    ),
    RiskReasonCode.RISK_CONFIGURATION_INCOMPLETE: (
        "Emitted by any rule whose mandatory threshold has never been "
        "calibrated, in place of that rule's own code."
    ),
}
