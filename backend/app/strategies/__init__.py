"""Strategy framework.

A strategy proposes; it never executes and never sizes. See
``app.strategies.base`` for how AC-19 is enforced structurally, and
``tests/test_architecture.py`` for the import-graph rule that backs it up.

Importing this package registers nothing. Call
``register_builtin_strategies()`` to populate a registry - running a strategy is
a decision, not a consequence of an import.
"""

from app.strategies.base import (
    SignalProposal,
    Strategy,
    StrategyContext,
    StrategyError,
    code_fingerprint,
)
from app.strategies.builtin import register_builtin_strategies
from app.strategies.registry import StrategyRegistry, registry
from app.strategies.trend_pullback import TrendPullbackParameters, TrendPullbackStrategy

__all__ = [
    "SignalProposal",
    "Strategy",
    "StrategyContext",
    "StrategyError",
    "StrategyRegistry",
    "TrendPullbackParameters",
    "TrendPullbackStrategy",
    "code_fingerprint",
    "register_builtin_strategies",
    "registry",
]
