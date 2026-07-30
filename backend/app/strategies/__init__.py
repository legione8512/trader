"""Strategy framework.

A strategy proposes; it never executes and never sizes. See
``app.strategies.base`` for how AC-19 is enforced structurally, and
``tests/test_architecture.py`` for the import-graph rule that backs it up.

No concrete strategy is registered here yet. The baseline strategy design is
reviewed before it is implemented (Phase 4.3).
"""

from app.strategies.base import (
    SignalProposal,
    Strategy,
    StrategyContext,
    StrategyError,
    code_fingerprint,
)
from app.strategies.registry import StrategyRegistry, registry

__all__ = [
    "SignalProposal",
    "Strategy",
    "StrategyContext",
    "StrategyError",
    "StrategyRegistry",
    "code_fingerprint",
    "registry",
]
