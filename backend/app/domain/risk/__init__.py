"""The risk engine.

Authoritative and deterministic: strategies propose, this decides. Pure - no
database, no exchange, no clock reads - so every rule can be exercised against a
constructed instant rather than a live one.

See docs/RISK_RULES.md for the rule table and the enforcement semantics.
"""

from app.domain.risk.context import (
    DayState,
    MarketState,
    ProposalUnderReview,
    RiskContext,
    SystemState,
)
from app.domain.risk.economics import TradingCosts, cost_in_r, net_reward_risk_ratio
from app.domain.risk.engine import RiskDecision, evaluate
from app.domain.risk.limits import RiskLimits
from app.domain.risk.rules import ALL_RULES, RiskRule, RuleOutcome

__all__ = [
    "ALL_RULES",
    "DayState",
    "MarketState",
    "ProposalUnderReview",
    "RiskContext",
    "RiskDecision",
    "RiskLimits",
    "RiskRule",
    "RuleOutcome",
    "SystemState",
    "TradingCosts",
    "cost_in_r",
    "evaluate",
    "net_reward_risk_ratio",
]
