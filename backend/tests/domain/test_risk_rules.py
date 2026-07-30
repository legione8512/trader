"""The risk rule table and the engine that walks it.

The first class here is the one that matters most: it checks the implemented
rules against docs/RISK_RULES.md. A documented protection with no code behind it
looks exactly like a protection, right up to the moment it is needed.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.domain.enums import (
    DailyPnlBasis,
    HealthStatus,
    OrderSide,
    RiskReasonCode,
    RiskVerdict,
    RuleAction,
    RuleStatus,
)
from app.domain.risk.context import (
    DayState,
    MarketState,
    ProposalUnderReview,
    RiskContext,
    SystemState,
)
from app.domain.risk.engine import RiskEngineError, evaluate
from app.domain.risk.limits import RiskLimits
from app.domain.risk.rules import ALL_RULES, CODES_EMITTED_ELSEWHERE, RuleOutcome

REPO_ROOT = Path(__file__).resolve().parents[3]
RISK_RULES_DOC = REPO_ROOT / "docs" / "RISK_RULES.md"

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

#: Rule identifiers that deliberately have no evaluating function.
RULES_WITHOUT_A_FUNCTION = {
    "R-01",  # the basis every percentage is computed from
    "R-25",  # rejected in Phase 0, kept so identifiers stay stable
    "R-26",  # selects which P&L figure R-03 compares; modifies a rule
}


def documented_rule_ids() -> set[str]:
    content = RISK_RULES_DOC.read_text(encoding="utf-8")
    return set(re.findall(r"^\| (R-\d{2}) \|", content, flags=re.MULTILINE))


#: Every mandatory gate calibrated, so a test can isolate one rule at a time.
#: Not the shipped configuration - see TestUncalibratedGates for that.
CALIBRATED = RiskLimits(
    reference_capital=Decimal("1000.00"),
    max_candle_age_seconds=1800,
    max_signal_age_seconds=300,
    max_spread_bps=Decimal("5"),
    min_order_book_depth_quote=Decimal("10000"),
    min_atr_percent=Decimal("0.15"),
    max_atr_percent=Decimal("2.00"),
    min_reward_risk_ratio=Decimal("1.8"),
    max_estimated_slippage_bps=Decimal("10"),
    max_clock_drift_ms=1000,
)

HEALTHY_MARKET = MarketState(
    candle_age=timedelta(seconds=60),
    spread_bps=Decimal("1.5"),
    order_book_depth_quote=Decimal("250000"),
    atr_percent=Decimal("0.60"),
    estimated_slippage_bps=Decimal("2"),
    clock_drift_ms=Decimal("120"),
)

GOOD_PROPOSAL = ProposalUnderReview(
    side=OrderSide.BUY,
    entry_price=Decimal("65000"),
    stop_loss_price=Decimal("64350"),
    take_profit_price=Decimal("66625"),
    net_reward_risk_ratio=Decimal("2.2"),
    quantity=Decimal("0.00176"),
    risk_amount_reporting=Decimal("4.83"),
    notional_quote=Decimal("114.4"),
    signal_age=timedelta(seconds=30),
)


def context(
    *,
    limits: RiskLimits = CALIBRATED,
    day: DayState | None = None,
    market: MarketState = HEALTHY_MARKET,
    system: SystemState | None = None,
    proposal: ProposalUnderReview | None = GOOD_PROPOSAL,
) -> RiskContext:
    return RiskContext(
        evaluated_at=NOW,
        limits=limits,
        day=day or DayState(time_remaining_in_day=timedelta(hours=6)),
        market=market,
        system=system or SystemState(),
        proposal=proposal,
    )


def outcome_for(rule_id: str, ctx: RiskContext) -> RuleOutcome:
    decision = evaluate(ctx)
    return next(item for item in decision.outcomes if item.rule_id == rule_id)


class TestTheTableMatchesTheSpecification:
    """A documented protection with no code behind it looks like a protection."""

    def test_every_documented_rule_is_implemented(self) -> None:
        implemented = {rule.rule_id for rule in ALL_RULES}
        expected = documented_rule_ids() - RULES_WITHOUT_A_FUNCTION
        assert implemented == expected, (
            f"Missing: {sorted(expected - implemented)}. "
            f"Unexpected: {sorted(implemented - expected)}."
        )

    def test_every_reason_code_is_reachable(self) -> None:
        """A code no rule can emit is a refusal that can never be explained,
        or a protection that can never fire."""
        from_rules = {rule.reason_code for rule in ALL_RULES}
        accounted = from_rules | set(CODES_EMITTED_ELSEWHERE)
        assert accounted == set(RiskReasonCode), (
            f"Unreachable codes: {sorted(code.value for code in set(RiskReasonCode) - accounted)}"
        )

    def test_no_two_rules_share_an_identifier(self) -> None:
        identifiers = [rule.rule_id for rule in ALL_RULES]
        assert len(identifiers) == len(set(identifiers))

    def test_every_rule_reports_its_own_identifier(self) -> None:
        """A rule filing its outcome under another rule's id would make the
        audit record point at the wrong protection."""
        ctx = context()
        for rule in ALL_RULES:
            assert rule(ctx).rule_id == rule.rule_id


class TestEveryRuleExplainsItself:
    def test_a_passing_rule_still_records_its_inputs(self) -> None:
        """ "It passed" is not an explanation. Section 3 of the specification
        requires the inputs and the parameters whatever the outcome."""
        decision = evaluate(context())
        for outcome in decision.outcomes:
            if outcome.status is RuleStatus.PASSED:
                assert outcome.inputs, f"{outcome.rule_id} passed without recording inputs"

    def test_a_triggered_rule_names_the_numbers_that_triggered_it(self) -> None:
        ctx = context(day=DayState(open_positions=1, time_remaining_in_day=timedelta(hours=6)))
        result = outcome_for("R-04", ctx)
        assert result.status is RuleStatus.TRIGGERED
        assert result.inputs["open_positions"] == "1"
        assert result.parameters["maximum_open_positions"] == "1"

    def test_the_record_shape_is_json_serialisable(self) -> None:
        decision = evaluate(context())
        payload = decision.as_evaluated_rules()
        assert payload["verdict"] == RiskVerdict.APPROVED.value
        rules = payload["rules"]
        assert isinstance(rules, list)
        assert len(rules) == len(ALL_RULES)


class TestApproval:
    def test_a_clean_proposal_is_approved(self) -> None:
        decision = evaluate(context())
        assert decision.verdict is RiskVerdict.APPROVED
        assert decision.reason_codes == ()
        assert decision.action is None

    def test_an_approval_explains_what_was_checked(self) -> None:
        """An approval nobody can explain is as useless as a refusal nobody
        can explain."""
        decision = evaluate(context())
        assert "Approved" in decision.explanation
        assert "R-02" in decision.explanation


class TestLimits:
    def test_risk_above_the_per_trade_budget_is_refused(self) -> None:
        proposal = ProposalUnderReview(
            side=OrderSide.BUY,
            entry_price=Decimal("65000"),
            stop_loss_price=Decimal("64350"),
            net_reward_risk_ratio=Decimal("2.2"),
            risk_amount_reporting=Decimal("5.01"),
            signal_age=timedelta(seconds=30),
        )
        decision = evaluate(context(proposal=proposal))
        assert RiskReasonCode.RISK_PER_TRADE_EXCEEDED in decision.reason_codes

    def test_an_unsized_proposal_is_refused_not_assumed_small(self) -> None:
        """Not knowing the risk is a reason to refuse, never a reason to
        assume it is within budget."""
        proposal = ProposalUnderReview(
            side=OrderSide.BUY,
            entry_price=Decimal("65000"),
            stop_loss_price=Decimal("64350"),
            net_reward_risk_ratio=Decimal("2.2"),
            risk_amount_reporting=None,
            signal_age=timedelta(seconds=30),
        )
        decision = evaluate(context(proposal=proposal))
        assert RiskReasonCode.RISK_PER_TRADE_EXCEEDED in decision.reason_codes

    def test_the_daily_loss_limit_halts_the_day(self) -> None:
        day = DayState(realised_pnl=Decimal("-40"), time_remaining_in_day=timedelta(hours=6))
        decision = evaluate(context(day=day))
        assert RiskReasonCode.DAILY_LOSS_LIMIT_REACHED in decision.reason_codes
        assert decision.action is RuleAction.HALT_DAY

    def test_an_unrealised_loss_alone_halts_the_day(self) -> None:
        """Decision OD-06 chose the conservative basis: a position sitting at
        -41 RON stops the day now, without waiting for it to close."""
        day = DayState(
            realised_pnl=Decimal("0"),
            unrealised_pnl=Decimal("-41"),
            time_remaining_in_day=timedelta(hours=6),
        )
        decision = evaluate(context(day=day))
        assert RiskReasonCode.DAILY_LOSS_LIMIT_REACHED in decision.reason_codes

    def test_the_realised_only_basis_ignores_an_open_loss(self) -> None:
        """R-26 changes what R-03 measures, and the record must show which."""
        limits = RiskLimits(
            reference_capital=Decimal("1000.00"),
            daily_pnl_basis=DailyPnlBasis.REALISED_ONLY,
            max_candle_age_seconds=1800,
            max_signal_age_seconds=300,
            max_spread_bps=Decimal("5"),
            min_order_book_depth_quote=Decimal("10000"),
            min_atr_percent=Decimal("0.15"),
            max_atr_percent=Decimal("2.00"),
            min_reward_risk_ratio=Decimal("1.8"),
            max_estimated_slippage_bps=Decimal("10"),
            max_clock_drift_ms=1000,
        )
        day = DayState(unrealised_pnl=Decimal("-41"), time_remaining_in_day=timedelta(hours=6))
        decision = evaluate(context(limits=limits, day=day))
        assert RiskReasonCode.DAILY_LOSS_LIMIT_REACHED not in decision.reason_codes

    def test_the_trade_count_limit_halts_the_day(self) -> None:
        day = DayState(trades_today=50, time_remaining_in_day=timedelta(hours=6))
        decision = evaluate(context(day=day))
        assert RiskReasonCode.MAX_TRADES_PER_DAY_REACHED in decision.reason_codes
        assert decision.action is RuleAction.HALT_DAY

    def test_three_consecutive_losses_halt_the_day(self) -> None:
        day = DayState(consecutive_losses=3, time_remaining_in_day=timedelta(hours=6))
        decision = evaluate(context(day=day))
        assert RiskReasonCode.MAX_CONSECUTIVE_LOSSES_REACHED in decision.reason_codes


class TestSessionRules:
    def test_reaching_the_target_closes_the_session(self) -> None:
        day = DayState(session_pnl=Decimal("20"), time_remaining_in_day=timedelta(hours=6))
        decision = evaluate(context(day=day))
        assert RiskReasonCode.SESSION_TARGET_REACHED in decision.reason_codes
        assert decision.action is RuleAction.CLOSE_SESSION

    def test_restart_eligibility_is_a_permission_not_a_refusal(self) -> None:
        """R-08 triggering means a new session MAY start. Treating every
        triggered rule as bad would invert it."""
        day = DayState(session_pnl=Decimal("45"), time_remaining_in_day=timedelta(hours=6))
        decision = evaluate(context(day=day))
        permissions = {item.rule_id for item in decision.permissions}
        assert "R-08" in permissions
        assert RiskReasonCode.SESSION_RESTART_ELIGIBLE not in decision.reason_codes

    def test_exactly_at_the_restart_threshold_is_not_eligible(self) -> None:
        """SRS 6.5: strictly above 4%. At exactly 4% the day stops."""
        day = DayState(session_pnl=Decimal("40"), time_remaining_in_day=timedelta(hours=6))
        decision = evaluate(context(day=day))
        assert "R-08" not in {item.rule_id for item in decision.permissions}


class TestMarketQualityGates:
    def test_a_stale_feed_refuses_the_order(self) -> None:
        market = MarketState(
            candle_age=timedelta(seconds=1801),
            spread_bps=Decimal("1.5"),
            order_book_depth_quote=Decimal("250000"),
            atr_percent=Decimal("0.60"),
            estimated_slippage_bps=Decimal("2"),
            clock_drift_ms=Decimal("120"),
        )
        decision = evaluate(context(market=market))
        assert RiskReasonCode.STALE_MARKET_DATA in decision.reason_codes

    def test_an_unmeasured_gate_refuses_rather_than_passes(self) -> None:
        """Not knowing is not the same as being fine."""
        decision = evaluate(context(market=MarketState()))
        for code in (
            RiskReasonCode.STALE_MARKET_DATA,
            RiskReasonCode.SPREAD_TOO_WIDE,
            RiskReasonCode.LIQUIDITY_TOO_LOW,
            RiskReasonCode.VOLATILITY_OUT_OF_RANGE,
            RiskReasonCode.ESTIMATED_SLIPPAGE_TOO_HIGH,
            RiskReasonCode.CLOCK_DRIFT_EXCEEDED,
        ):
            assert code in decision.reason_codes

    def test_clock_drift_is_judged_on_its_absolute_value(self) -> None:
        """A clock that is behind is as dangerous as one that is ahead."""
        for drift in (Decimal("1500"), Decimal("-1500")):
            market = MarketState(
                candle_age=timedelta(seconds=60),
                spread_bps=Decimal("1.5"),
                order_book_depth_quote=Decimal("250000"),
                atr_percent=Decimal("0.60"),
                estimated_slippage_bps=Decimal("2"),
                clock_drift_ms=drift,
            )
            decision = evaluate(context(market=market))
            assert RiskReasonCode.CLOCK_DRIFT_EXCEEDED in decision.reason_codes

    def test_a_gross_reward_ratio_is_never_accepted_in_place_of_the_net_one(self) -> None:
        proposal = ProposalUnderReview(
            side=OrderSide.BUY,
            entry_price=Decimal("65000"),
            stop_loss_price=Decimal("64350"),
            take_profit_price=Decimal("66625"),
            net_reward_risk_ratio=None,
            risk_amount_reporting=Decimal("4.83"),
            signal_age=timedelta(seconds=30),
        )
        decision = evaluate(context(proposal=proposal))
        assert RiskReasonCode.REWARD_RISK_TOO_LOW in decision.reason_codes


class TestUncalibratedGates:
    """The shipped configuration leaves several gates uncalibrated on purpose."""

    def test_an_uncalibrated_gate_refuses(self) -> None:
        decision = evaluate(context(limits=RiskLimits(reference_capital=Decimal("1000.00"))))
        assert decision.verdict is RiskVerdict.REJECTED
        assert RiskReasonCode.RISK_CONFIGURATION_INCOMPLETE in decision.reason_codes

    def test_it_is_reported_under_its_own_code_not_the_gates(self) -> None:
        """SPREAD_TOO_WIDE when nobody set a maximum spread would tell an
        operator the market was bad, when the configuration is missing."""
        decision = evaluate(context(limits=RiskLimits(reference_capital=Decimal("1000.00"))))
        assert RiskReasonCode.SPREAD_TOO_WIDE not in decision.reason_codes

    def test_the_uncalibrated_rules_are_listed_separately(self) -> None:
        """They clear by finishing the calibration work, not by waiting for the
        market to improve."""
        decision = evaluate(context(limits=RiskLimits(reference_capital=Decimal("1000.00"))))
        assert set(decision.uncalibrated_rules) == {
            "R-09",
            "R-10",
            "R-11",
            "R-12",
            "R-13",
            "R-14",
            "R-15",
            "R-22",
        }

    def test_a_disabled_rule_is_not_an_uncalibrated_one(self) -> None:
        """R-23 is NULL because decision OD-03 switched it off. It reports
        NOT_APPLICABLE and does not block."""
        result = outcome_for("R-23", context())
        assert result.status is RuleStatus.NOT_APPLICABLE
        assert "OD-03" in result.detail

    def test_an_enabled_profit_floor_does_block(self) -> None:
        limits = RiskLimits(
            reference_capital=Decimal("1000.00"),
            daily_profit_giveback_percent=Decimal("50.00"),
            max_candle_age_seconds=1800,
            max_signal_age_seconds=300,
            max_spread_bps=Decimal("5"),
            min_order_book_depth_quote=Decimal("10000"),
            min_atr_percent=Decimal("0.15"),
            max_atr_percent=Decimal("2.00"),
            min_reward_risk_ratio=Decimal("1.8"),
            max_estimated_slippage_bps=Decimal("10"),
            max_clock_drift_ms=1000,
        )
        day = DayState(
            realised_pnl=Decimal("10"),
            peak_pnl=Decimal("30"),
            time_remaining_in_day=timedelta(hours=6),
        )
        decision = evaluate(context(limits=limits, day=day))
        assert RiskReasonCode.DAILY_PROFIT_FLOOR_REACHED in decision.reason_codes


class TestSystemGates:
    def test_the_emergency_stop_refuses_everything(self) -> None:
        decision = evaluate(context(system=SystemState(emergency_stop_active=True)))
        assert decision.action is RuleAction.REJECT_ALL
        assert RiskReasonCode.EMERGENCY_STOP_ACTIVE in decision.reason_codes

    def test_degraded_health_refuses_new_entries(self) -> None:
        decision = evaluate(context(system=SystemState(health=HealthStatus.DEGRADED)))
        assert RiskReasonCode.SYSTEM_HEALTH_DEGRADED in decision.reason_codes

    def test_a_disabled_strategy_is_refused(self) -> None:
        decision = evaluate(context(system=SystemState(strategy_enabled=False)))
        assert RiskReasonCode.STRATEGY_DISABLED in decision.reason_codes

    def test_a_sizing_failure_is_reported_not_recomputed(self) -> None:
        """The filter arithmetic lives in position sizing. A second
        implementation here could disagree with the first."""
        system = SystemState(
            sizing_is_viable=False,
            sizing_reason_codes=(RiskReasonCode.MIN_NOTIONAL_NOT_MET.value,),
        )
        decision = evaluate(context(system=system))
        assert RiskReasonCode.EXCHANGE_FILTER_VIOLATION in decision.reason_codes

    def test_an_insufficient_balance_surfaces_through_its_own_rule(self) -> None:
        system = SystemState(
            sizing_is_viable=False,
            sizing_reason_codes=(RiskReasonCode.INSUFFICIENT_BALANCE.value,),
        )
        decision = evaluate(context(system=system))
        assert RiskReasonCode.INSUFFICIENT_BALANCE in decision.reason_codes


class TestDayBoundary:
    def test_entries_stop_before_the_day_ends(self) -> None:
        day = DayState(time_remaining_in_day=timedelta(minutes=29))
        decision = evaluate(context(day=day))
        assert RiskReasonCode.DAY_BOUNDARY_NO_ENTRY_WINDOW in decision.reason_codes

    def test_an_unknown_remaining_time_refuses(self) -> None:
        day = DayState(time_remaining_in_day=None)
        decision = evaluate(context(day=day))
        assert RiskReasonCode.DAY_BOUNDARY_NO_ENTRY_WINDOW in decision.reason_codes


class TestAggregation:
    def test_every_rule_runs_even_after_one_has_refused(self) -> None:
        """A record naming one problem when there were five makes a system look
        one fix away from trading when it is not."""
        decision = evaluate(
            context(
                day=DayState(open_positions=5, trades_today=99),
                system=SystemState(emergency_stop_active=True, exchange_healthy=False),
            )
        )
        assert len(decision.outcomes) == len(ALL_RULES)
        assert len(decision.reason_codes) >= 4

    def test_the_furthest_reaching_consequence_wins(self) -> None:
        decision = evaluate(
            context(
                day=DayState(open_positions=5, time_remaining_in_day=timedelta(hours=6)),
                system=SystemState(emergency_stop_active=True),
            )
        )
        assert decision.action is RuleAction.REJECT_ALL

    def test_reason_codes_are_deduplicated(self) -> None:
        """Two rules reporting the same code would read like two problems."""
        decision = evaluate(context(limits=RiskLimits(reference_capital=Decimal("1000.00"))))
        assert len(decision.reason_codes) == len(set(decision.reason_codes))

    def test_a_rejection_always_carries_a_reason(self) -> None:
        """The database CHECK refuses a rejection with no reason, and rightly:
        a refusal nobody can explain cannot be acted on."""
        decision = evaluate(context(day=DayState(open_positions=1)))
        assert decision.verdict is RiskVerdict.REJECTED
        assert decision.reason_codes

    def test_a_blocking_rule_without_a_code_fails_loudly(self) -> None:
        """Failing here points at the rule that produced it, instead of at the
        INSERT that the database refused."""
        from app.domain.risk.rules import RiskRule, RuleOutcome

        broken = RiskRule(
            rule_id="R-99",
            name="Broken",
            reason_code=RiskReasonCode.NO_VALID_OPPORTUNITY,
            action=RuleAction.REJECT_ORDER,
            evaluate=lambda _: RuleOutcome(
                rule_id="R-99",
                name="Broken",
                status=RuleStatus.TRIGGERED,
                action=RuleAction.REJECT_ORDER,
                reason_code=None,
            ),
        )
        with pytest.raises(RiskEngineError, match="without a reason code"):
            evaluate(context(), rules=[broken])


class TestDayLevelEvaluation:
    def test_rules_needing_a_proposal_skip_cleanly(self) -> None:
        """ "May a session start now" is a real question with no proposal to
        judge. Skipping is not the same as passing, and the record says which."""
        decision = evaluate(context(proposal=None))
        for rule_id in ("R-02", "R-10", "R-14"):
            result = next(item for item in decision.outcomes if item.rule_id == rule_id)
            assert result.status is RuleStatus.NOT_APPLICABLE

    def test_day_level_gates_still_apply_without_a_proposal(self) -> None:
        day = DayState(consecutive_losses=3, time_remaining_in_day=timedelta(hours=6))
        decision = evaluate(context(day=day, proposal=None))
        assert RiskReasonCode.MAX_CONSECUTIVE_LOSSES_REACHED in decision.reason_codes
