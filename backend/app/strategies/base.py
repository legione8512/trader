"""The strategy contract.

Everything in this module exists to make one acceptance criterion structural
rather than aspirational:

    **AC-19 - a strategy cannot bypass the risk engine.**

It is enforced three ways here, each of which would have to be defeated
separately:

1. **A strategy proposes; it never sizes.** ``SignalProposal`` has no quantity
   field. A strategy literally cannot express "buy 0.5 BTC" - the most it can
   say is "this looks like a long, here is where I would be wrong". Position
   size is computed by the risk engine from the stop distance and the risk
   budget, and nowhere else.

2. **``evaluate`` is synchronous.** Every I/O path in this application is
   ``async``: the exchange client, the repositories, the session. A synchronous
   function cannot await any of them, so a strategy cannot reach the exchange or
   the database even if someone imported them. The import-graph test forbids the
   import; the signature makes the import useless.

3. **A strategy sees candles and nothing else.** ``StrategyContext`` carries no
   balance, no open position, no order book and no adapter. A strategy that
   cannot see the balance cannot size against it, and one that cannot see the
   open position cannot decide to add to it.

Returning ``None`` from ``evaluate`` is a first-class, expected result. The
system is specified to refuse to trade when no sufficiently strong opportunity
exists, so "no signal" is an answer, not a failure.
"""

from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Any, Protocol, runtime_checkable

from app.domain.candle_window import CandleWindow
from app.domain.enums import OrderSide, Timeframe
from app.domain.errors import DomainError
from app.domain.money import CALCULATION_PRECISION, quantize_price


class StrategyError(DomainError):
    """A strategy produced something it is not allowed to produce."""


@dataclass(frozen=True, slots=True)
class StrategyContext:
    """Everything a strategy is allowed to see.

    Deliberately small. Each field that is absent is absent for a reason, and
    the reasons are in the module docstring.
    """

    #: Closed candles, oldest first, contiguous and aligned.
    window: CandleWindow
    #: The instant the evaluation is happening. Injected, never read from the
    #: wall clock, so a backtest and a live run take the same code path.
    evaluated_at: datetime

    @property
    def symbol(self) -> str:
        return self.window.symbol

    @property
    def timeframe(self) -> Timeframe:
        return self.window.timeframe

    def __post_init__(self) -> None:
        if self.evaluated_at.tzinfo is None:
            raise StrategyError("evaluated_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class SignalProposal:
    """What a strategy may propose. Never an instruction to trade.

    There is no quantity here, and that is the point. A strategy states a
    direction, a price it is reasoning about and a price at which it would be
    wrong. Turning that into a number of coins is the risk engine's job, because
    only the risk engine knows the risk budget, the daily loss so far and the
    exchange's filters.
    """

    side: OrderSide
    #: The price the reasoning is anchored to, normally the last close.
    reference_price: Decimal
    #: Where this idea is proven wrong. Not optional: a proposal without an
    #: invalidation point cannot be sized, because 1R would be undefined.
    stop_loss_price: Decimal
    #: Optional. A strategy may legitimately manage the exit by other means.
    take_profit_price: Decimal | None = None

    #: An internal RANKING score, never a probability. Presenting it as one
    #: would require statistical calibration that has not been done.
    confidence_score: Decimal | None = None
    #: The components behind the score. A total alone cannot be audited.
    score_components: dict[str, Any] = field(default_factory=dict)
    #: The indicator values the strategy actually saw. Without these the
    #: decision can be guessed at but not replayed.
    inputs: dict[str, Any] = field(default_factory=dict)
    #: Open time of the candle the decision was taken on.
    candle_open_time: datetime | None = None

    def __post_init__(self) -> None:
        # Normalised to the precision the system can persist, BEFORE validation.
        #
        # A proposal carrying more digits than the column holds would be stored
        # as a different number from the one the strategy decided on, and the
        # audit trail would no longer be the decision. Validating afterwards
        # matters too: rounding can in principle collapse a stop onto the entry,
        # and that must be caught rather than stored.
        object.__setattr__(self, "reference_price", quantize_price(self.reference_price))
        object.__setattr__(self, "stop_loss_price", quantize_price(self.stop_loss_price))
        if self.take_profit_price is not None:
            object.__setattr__(self, "take_profit_price", quantize_price(self.take_profit_price))

        if self.reference_price <= 0:
            raise StrategyError(f"Reference price must be positive: {self.reference_price}")
        if self.stop_loss_price <= 0:
            raise StrategyError(f"Stop loss price must be positive: {self.stop_loss_price}")

        # The same rule the database CHECK enforces, caught here instead - at
        # the place where the bug actually is. A long whose stop sits above the
        # entry is not a strategy choice, it is an inverted sign, and it would
        # make the risk engine compute a negative 1R.
        if self.side is OrderSide.BUY:
            if self.stop_loss_price >= self.reference_price:
                raise StrategyError(
                    f"A long stop must be below the entry: "
                    f"stop {self.stop_loss_price} >= reference {self.reference_price}"
                )
            if self.take_profit_price is not None and (
                self.take_profit_price <= self.reference_price
            ):
                raise StrategyError(
                    f"A long target must be above the entry: "
                    f"target {self.take_profit_price} <= reference {self.reference_price}"
                )
        elif self.side is OrderSide.SELL:
            if self.stop_loss_price <= self.reference_price:
                raise StrategyError(
                    f"A short stop must be above the entry: "
                    f"stop {self.stop_loss_price} <= reference {self.reference_price}"
                )
            if self.take_profit_price is not None and (
                self.take_profit_price >= self.reference_price
            ):
                raise StrategyError(
                    f"A short target must be below the entry: "
                    f"target {self.take_profit_price} >= reference {self.reference_price}"
                )

    @property
    def stop_distance(self) -> Decimal:
        """Absolute distance to the stop: the 1R denominator for sizing."""
        return abs(self.reference_price - self.stop_loss_price)

    @property
    def reward_risk_ratio(self) -> Decimal | None:
        """Gross reward-to-risk. ``None`` without a target.

        Gross, and named so. The ratio that decides anything (rule R-14) is net
        of estimated fees and slippage, and only the risk engine knows those.

        **Not exactly the strategy's configured multiple, and it should not be.**
        The prices are normalised to storage precision, so a target built as
        ``entry + 2.5 * distance`` comes back as 2.49999999999... once re-derived.
        That is the ratio the position will actually have, and it is the honest
        number to judge. Any rule comparing it against a threshold must allow
        for it rather than expect equality - and it will move further once
        execution rounds to the exchange tick.
        """
        if self.take_profit_price is None:
            return None
        distance = self.stop_distance
        if distance == 0:
            return None
        with localcontext() as arithmetic:
            arithmetic.prec = CALCULATION_PRECISION
            return abs(self.take_profit_price - self.reference_price) / distance

    def __repr__(self) -> str:
        return (
            f"<SignalProposal {self.side.value} @{self.reference_price} "
            f"stop={self.stop_loss_price} target={self.take_profit_price}>"
        )


@runtime_checkable
class Strategy(Protocol):
    """A decision procedure over closed candles."""

    @property
    def name(self) -> str:
        """Stable identifier. Matches the ``strategy.name`` column."""
        ...

    @property
    def parameters(self) -> dict[str, Any]:
        """The exact parameter set, as it will be stored on the version.

        Must be JSON-serialisable and complete: a parameter that is not in here
        cannot be reproduced from the record, which breaks AC-20.
        """
        ...

    @property
    def required_candles(self) -> int:
        """Minimum window length before this strategy may be evaluated.

        The strategy's own responsibility, because only it knows the longest
        indicator period it uses.
        """
        ...

    def evaluate(self, context: StrategyContext) -> SignalProposal | None:
        """Propose a trade, or ``None`` when nothing qualifies.

        Synchronous on purpose. See the module docstring.
        """
        ...


def code_fingerprint(strategy: Strategy | type) -> str:
    """SHA-256 of the strategy's own source code.

    Two runs with the same parameters but different code are not the same
    experiment. Without this, a backtest result could be attributed to a
    parameter set that a later edit quietly changed the meaning of, and AC-20
    ("a backtest is reproducible") would be unverifiable.

    Only the class body is hashed, not its dependencies. That is a stated
    limitation rather than an oversight: hashing the transitive closure would
    change the fingerprint whenever an unrelated module was reformatted, and a
    fingerprint that changes for no reason is one people learn to ignore.
    """
    target = strategy if isinstance(strategy, type) else type(strategy)
    try:
        source = inspect.getsource(target)
    except (OSError, TypeError) as exc:
        raise StrategyError(
            f"Cannot read the source of {target.__name__} to fingerprint it. "
            f"A strategy whose code cannot be identified cannot be reproduced."
        ) from exc
    return hashlib.sha256(source.encode("utf-8")).hexdigest()
