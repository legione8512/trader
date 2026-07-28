"""Domain-level exceptions.

Part of the pure domain layer: no I/O, no framework, no infrastructure.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for every violation of a domain rule."""


class MoneyError(DomainError):
    """An invalid monetary operation."""


class CurrencyMismatchError(MoneyError):
    """Two amounts in different currencies were combined.

    This is the error that stops 1,000 RON from being silently treated as
    1,000 USDT. Conversion must always be explicit and must always go through a
    recorded FX rate.
    """

    def __init__(self, left: str, right: str, operation: str) -> None:
        self.left = left
        self.right = right
        self.operation = operation
        super().__init__(
            f"Cannot {operation} amounts in different currencies: {left} and {right}. "
            f"Convert explicitly through a recorded FX rate."
        )


class InvalidTransitionError(DomainError):
    """A state machine was asked for a transition that is not allowed."""

    def __init__(self, machine: str, current: str, target: str, allowed: tuple[str, ...]) -> None:
        self.machine = machine
        self.current = current
        self.target = target
        self.allowed = allowed
        allowed_text = ", ".join(allowed) if allowed else "none (terminal state)"
        super().__init__(
            f"{machine}: illegal transition {current} -> {target}. Allowed from "
            f"{current}: {allowed_text}."
        )
