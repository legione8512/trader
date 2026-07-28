"""Enum tests, including a docs-versus-code consistency check."""

from __future__ import annotations

import re
from pathlib import Path

from app.domain.enums import (
    AutonomyMode,
    ExecutionVenue,
    HealthStatus,
    OrderStatus,
    RiskReasonCode,
    Timeframe,
    TradingOutcome,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
RISK_RULES_DOC = REPO_ROOT / "docs" / "RISK_RULES.md"


def reason_codes_documented() -> set[str]:
    """Extract the reason-code block from docs/RISK_RULES.md."""
    content = RISK_RULES_DOC.read_text(encoding="utf-8")
    blocks = re.findall(r"```text\n(.*?)```", content, flags=re.DOTALL)
    for block in blocks:
        if "RISK_PER_TRADE_EXCEEDED" in block:
            return {line.strip() for line in block.splitlines() if line.strip()}
    raise AssertionError("Reason code block not found in docs/RISK_RULES.md")


class TestReasonCodesMatchDocumentation:
    """Documentation that drifts from code is worse than no documentation.

    Reason codes appear in the audit trail and are what an operator reads when
    a trade was refused. If the list in the specification and the list in the
    code disagree, one of them is lying.
    """

    def test_the_documentation_file_exists(self) -> None:
        assert RISK_RULES_DOC.is_file(), f"Missing {RISK_RULES_DOC}"

    def test_code_and_documentation_list_the_same_codes(self) -> None:
        assert {code.value for code in RiskReasonCode} == reason_codes_documented()

    def test_every_code_is_screaming_snake_case(self) -> None:
        for code in RiskReasonCode:
            assert re.fullmatch(r"[A-Z][A-Z0-9_]*", code.value), code.value

    def test_the_name_equals_the_value(self) -> None:
        """So a code read from the database maps back unambiguously."""
        for code in RiskReasonCode:
            assert code.name == code.value


class TestAutonomyMode:
    def test_only_live_submits_real_orders(self) -> None:
        assert AutonomyMode.SIGNAL_ONLY.submits_real_orders is False
        assert AutonomyMode.PAPER_AUTOMATIC.submits_real_orders is False
        assert AutonomyMode.LIVE_AUTOMATIC.submits_real_orders is True

    def test_there_are_exactly_three_modes(self) -> None:
        assert len(AutonomyMode) == 3


class TestExecutionVenue:
    def test_paper_and_live_are_distinct_recorded_values(self) -> None:
        """A paper fill must never be mistakable for a real one, ever.

        mypy proves ``PAPER != LIVE`` statically, so asserting it at runtime is
        vacuous. What is worth asserting is that the persisted VALUES stay
        distinct: those are what a report reads back months later.
        """
        assert {venue.value for venue in ExecutionVenue} == {"PAPER", "LIVE", "BACKTEST"}
        assert len({venue.value for venue in ExecutionVenue}) == len(ExecutionVenue)


class TestHealthStatusAggregation:
    def test_no_checks_is_reported_as_starting_not_healthy(self) -> None:
        assert HealthStatus.worst([]) is HealthStatus.STARTING

    def test_the_worst_status_wins(self) -> None:
        assert HealthStatus.worst([HealthStatus.HEALTHY, HealthStatus.HEALTHY]) is (
            HealthStatus.HEALTHY
        )
        assert HealthStatus.worst([HealthStatus.HEALTHY, HealthStatus.DEGRADED]) is (
            HealthStatus.DEGRADED
        )
        assert HealthStatus.worst([HealthStatus.DEGRADED, HealthStatus.UNHEALTHY]) is (
            HealthStatus.UNHEALTHY
        )
        assert HealthStatus.worst([HealthStatus.STARTING, HealthStatus.HEALTHY]) is (
            HealthStatus.STARTING
        )


class TestOrderStatusProperties:
    def test_open_on_exchange(self) -> None:
        open_states = {status for status in OrderStatus if status.is_open_on_exchange}
        assert open_states == {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}

    def test_only_unknown_requires_reconciliation(self) -> None:
        needing = {status for status in OrderStatus if status.requires_reconciliation}
        assert needing == {OrderStatus.UNKNOWN}


class TestTimeframe:
    def test_the_primary_timeframe_is_fifteen_minutes(self) -> None:
        assert Timeframe.M15.value == "15m"
        assert Timeframe.M15.minutes == 15

    def test_minutes_are_consistent(self) -> None:
        assert Timeframe.H1.minutes == 60
        assert Timeframe.H4.minutes == 240
        assert Timeframe.D1.minutes == 1440


class TestTradingOutcome:
    def test_no_trade_is_a_first_class_outcome(self) -> None:
        """Not an error, not a failure mode. A normal, expected result.

        The point is structural: NO_TRADE is its own outcome, separate from
        every failure outcome, so a day with no opportunities can never be
        reported as a malfunction.
        """
        failure_outcomes = {
            TradingOutcome.TECHNICAL_FAILURE,
            TradingOutcome.DAILY_STOP_REACHED,
            TradingOutcome.MANUALLY_STOPPED,
        }
        assert TradingOutcome.NO_TRADE not in failure_outcomes
