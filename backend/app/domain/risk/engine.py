"""The risk engine.

Walks every rule, collects every outcome, and aggregates them into one verdict.
It contains no rule logic of its own - deliberately, because a rule that lives
in the aggregator is a rule the rule table cannot see.

**Every rule is evaluated, even after one has already refused.** Short-circuiting
would be faster and would produce a record that names one problem when there were
five. An operator deciding whether to clear a condition needs to know all of
them, and a report that lists only the first refusal makes a system look one fix
away from trading when it is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.domain.enums import RiskReasonCode, RiskVerdict, RuleAction, RuleStatus
from app.domain.errors import DomainError
from app.domain.risk.context import RiskContext
from app.domain.risk.rules import ALL_RULES, RiskRule, RuleOutcome


class RiskEngineError(DomainError):
    """The engine reached a state it must never reach."""


@dataclass(frozen=True, slots=True)
class RiskDecision:
    """The verdict, and everything needed to defend it months later."""

    verdict: RiskVerdict
    reason_codes: tuple[RiskReasonCode, ...]
    #: The furthest-reaching consequence among the rules that blocked. ``None``
    #: on an approval.
    action: RuleAction | None
    outcomes: tuple[RuleOutcome, ...]
    explanation: str

    @property
    def is_approved(self) -> bool:
        return self.verdict is RiskVerdict.APPROVED

    @property
    def blocking(self) -> tuple[RuleOutcome, ...]:
        return tuple(outcome for outcome in self.outcomes if outcome.blocks)

    @property
    def permissions(self) -> tuple[RuleOutcome, ...]:
        """Rules that triggered without refusing - today, only R-08."""
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status is RuleStatus.TRIGGERED and not outcome.action.refuses_the_proposal
        )

    @property
    def uncalibrated_rules(self) -> tuple[str, ...]:
        """Which gates refused because nobody has measured them yet.

        Worth separating from real refusals: these clear by finishing the
        calibration work, not by waiting for the market to improve.
        """
        return tuple(
            outcome.rule_id
            for outcome in self.outcomes
            if outcome.status is RuleStatus.NOT_CALIBRATED
        )

    def as_evaluated_rules(self) -> dict[str, object]:
        """The JSONB payload for ``RiskAssessment.evaluated_rules``.

        Every rule, not only the ones that refused. Section 3 of
        docs/RISK_RULES.md requires it: an approval that cannot be explained is
        as useless as a refusal that cannot be.
        """
        return {
            "verdict": self.verdict.value,
            "action": self.action.value if self.action else None,
            "rules": [outcome.as_record() for outcome in self.outcomes],
        }


def evaluate(context: RiskContext, rules: Sequence[RiskRule] = ALL_RULES) -> RiskDecision:
    """Run every rule and aggregate the result."""
    outcomes = tuple(rule(context) for rule in rules)
    blocking = tuple(outcome for outcome in outcomes if outcome.blocks)

    if not blocking:
        return RiskDecision(
            verdict=RiskVerdict.APPROVED,
            reason_codes=(),
            action=None,
            outcomes=outcomes,
            explanation=_approval_explanation(outcomes),
        )

    # De-duplicated but order-preserving: two rules can report the same code,
    # and a record listing it twice reads like two separate problems.
    codes: list[RiskReasonCode] = []
    for outcome in blocking:
        if outcome.reason_code is not None and outcome.reason_code not in codes:
            codes.append(outcome.reason_code)

    if not codes:
        # The database CHECK refuses a rejection with no reason, and rightly:
        # a refusal nobody can explain cannot be acted on. Failing here points
        # at the rule that produced it instead of at the INSERT.
        raise RiskEngineError(
            f"Rules {[o.rule_id for o in blocking]} blocked without a reason code."
        )

    action = max((outcome.action for outcome in blocking), key=lambda item: item.reach)
    return RiskDecision(
        verdict=RiskVerdict.REJECTED,
        reason_codes=tuple(codes),
        action=action,
        outcomes=outcomes,
        explanation=_rejection_explanation(blocking, action),
    )


def _rejection_explanation(blocking: Sequence[RuleOutcome], action: RuleAction) -> str:
    lines = [f"Refused. Consequence: {action.value}."]
    lines.extend(
        f"  {outcome.rule_id} {outcome.name}: {outcome.detail or outcome.status.value}"
        for outcome in blocking
    )
    return "\n".join(lines)


def _approval_explanation(outcomes: Sequence[RuleOutcome]) -> str:
    checked = sum(1 for outcome in outcomes if outcome.status is RuleStatus.PASSED)
    skipped = sum(1 for outcome in outcomes if outcome.status is RuleStatus.NOT_APPLICABLE)
    lines = [f"Approved. {checked} rule(s) checked, {skipped} not applicable."]
    lines.extend(
        f"  {outcome.rule_id} {outcome.name}: {outcome.detail}"
        for outcome in outcomes
        if outcome.status is RuleStatus.PASSED and outcome.detail
    )
    return "\n".join(lines)
