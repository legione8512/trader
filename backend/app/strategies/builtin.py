"""Registration of the strategies this build ships with.

An explicit function rather than a side effect of importing a package. A
strategy that runs because someone happened to import a module is a strategy
nobody decided to run, and the decision to run one is an operator decision.
"""

from __future__ import annotations

from app.strategies.registry import StrategyRegistry
from app.strategies.registry import registry as process_registry
from app.strategies.trend_pullback import TrendPullbackStrategy


def register_builtin_strategies(target: StrategyRegistry | None = None) -> StrategyRegistry:
    """Register every shipped strategy. Idempotent per registry instance.

    Registering twice into the same registry raises, on purpose: a duplicate
    name would make every stored signal ambiguous about which code produced it.
    Call this once, at startup.
    """
    registry = target if target is not None else process_registry
    registry.register(TrendPullbackStrategy.NAME, TrendPullbackStrategy.from_parameters)
    return registry
