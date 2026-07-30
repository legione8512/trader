"""Strategy registry.

Maps a stored ``strategy.name`` back to the code that implements it. Without
this, loading a signal from six months ago tells you which name produced it and
nothing about how.

Registration is explicit rather than by scanning the package. A strategy that
runs because it happened to be importable is a strategy nobody decided to run.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from typing import Any

from app.strategies.base import Strategy, StrategyError, code_fingerprint

#: Builds a strategy from a stored parameter set.
StrategyFactory = Callable[[Mapping[str, Any]], Strategy]


class StrategyRegistry:
    """The strategies this build knows how to run."""

    def __init__(self) -> None:
        self._factories: dict[str, StrategyFactory] = {}

    def register(self, name: str, factory: StrategyFactory) -> None:
        """Register a strategy under a name.

        A duplicate name is refused rather than overwritten. Two strategies
        sharing a name would make every stored signal ambiguous about which one
        produced it, and the second registration would silently win.
        """
        if not name:
            raise StrategyError("A strategy name cannot be empty")
        if name in self._factories:
            raise StrategyError(f"A strategy is already registered as {name!r}")
        self._factories[name] = factory

    def create(self, name: str, parameters: Mapping[str, Any] | None = None) -> Strategy:
        """Instantiate a registered strategy with a stored parameter set."""
        factory = self._factories.get(name)
        if factory is None:
            raise StrategyError(
                f"No strategy is registered as {name!r}. Known: {sorted(self._factories)}"
            )
        strategy = factory(parameters or {})
        if strategy.name != name:
            # Otherwise a signal would be filed under one name and replayed
            # under another.
            raise StrategyError(
                f"Strategy registered as {name!r} reports its name as {strategy.name!r}"
            )
        return strategy

    def fingerprint(self, name: str) -> str:
        """The code fingerprint of a registered strategy."""
        return code_fingerprint(self.create(name))

    @property
    def names(self) -> list[str]:
        return sorted(self._factories)

    def __contains__(self, name: object) -> bool:
        return name in self._factories

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(self._factories))

    def __len__(self) -> int:
        return len(self._factories)


#: The process-wide registry. Strategies are added to it in Phase 4.3, after
#: their design has been reviewed - not by importing a module.
registry = StrategyRegistry()
