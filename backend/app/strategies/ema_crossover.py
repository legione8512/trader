"""EMA crossover with a volatility-normalised trend filter. Long only.

The hypothesis under test: a fast EMA above a slow one identifies a trend worth
riding, and the trade is worth taking only when the gap between them is large
relative to the market's own noise.

**The trend filter is the part that matters.** A raw crossover fires constantly
in a sideways market, and every one of those is a round trip's worth of fees for
nothing. Dividing the gap by ATR makes the threshold mean the same thing in a
quiet market and a violent one:

    trendStrength = (EMA_fast - EMA_slow) / ATR

A gap of 100 dollars is a strong signal on an asset that moves 50 dollars a day
and noise on one that moves 500. Only the ratio can be compared.

**Long or flat, never short**, because the venue is spot (decision OD-15).

The take-profit is deliberately far and the trailing stop does the real work.
That is a claim the strategy makes and rule R-14 checks: a target that could not
plausibly be reached would let R-14 approve a trade whose realistic reward never
justified its cost.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from decimal import Decimal, localcontext
from typing import Any

from app.domain.enums import OrderSide, Timeframe
from app.domain.indicators import average_true_range, exponential_moving_average
from app.domain.money import CALCULATION_PRECISION, to_decimal
from app.strategies.base import SignalProposal, StrategyContext, StrategyError

ZERO = Decimal(0)


@dataclass(frozen=True, slots=True)
class EmaCrossoverParameters:
    """One parameter set. Immutable, stored verbatim on the version."""

    fast_period: int = 20
    slow_period: int = 100
    atr_period: int = 14

    #: Minimum (EMA_fast - EMA_slow) / ATR before an entry is allowed. Zero
    #: would admit every crossover, including the ones that reverse on the next
    #: candle and cost a round trip for nothing.
    min_trend_strength: Decimal = Decimal("0.10")

    #: Initial stop, in ATR below the entry.
    stop_atr_multiple: Decimal = Decimal("2.0")
    #: Distance the trailing stop keeps behind the best close, once armed.
    trailing_atr_multiple: Decimal = Decimal("2.5")
    #: How far the trade must move before the trail arms at all. Arming
    #: immediately would turn every small wobble into an exit.
    trailing_activation_atr: Decimal = Decimal("1.0")

    #: A ceiling rather than an expectation. The trailing stop is the real exit.
    reward_risk_target: Decimal = Decimal("3.0")

    #: See TrendPullbackParameters.warmup_margin_candles: the EMA seed is a
    #: simple average whose weight decays as (1 - alpha)^n, and a slow period of
    #: 100 needs roughly this much to bring it under 5%.
    warmup_margin_candles: int = 200

    #: Volatility gate, as fractions of price. Scaled to the bar length by
    #: scaled_for; see the note there.
    min_atr_fraction: Decimal = Decimal("0.0015")
    max_atr_fraction: Decimal = Decimal("0.0200")

    #: Capital floor: below this the notional would exceed the reference
    #: capital, which needs leverage. Never scaled with the timeframe.
    min_stop_fraction: Decimal = Decimal("0.005")
    max_stop_fraction: Decimal = Decimal("0.0300")

    def __post_init__(self) -> None:
        for name in ("fast_period", "slow_period", "atr_period", "warmup_margin_candles"):
            if getattr(self, name) < 1:
                raise StrategyError(f"{name} must be at least 1")
        if self.fast_period >= self.slow_period:
            raise StrategyError(
                f"Fast period must be shorter than slow: {self.fast_period} >= {self.slow_period}"
            )
        if self.min_trend_strength < ZERO:
            raise StrategyError("min_trend_strength cannot be negative")
        if self.stop_atr_multiple <= ZERO or self.trailing_atr_multiple <= ZERO:
            raise StrategyError("ATR multiples must be positive")
        if self.reward_risk_target <= ZERO:
            raise StrategyError("reward_risk_target must be positive")
        if self.min_stop_fraction <= ZERO or self.min_stop_fraction >= self.max_stop_fraction:
            raise StrategyError("Stop fraction bounds must satisfy 0 < min < max")
        if self.min_atr_fraction <= ZERO or self.min_atr_fraction >= self.max_atr_fraction:
            raise StrategyError("ATR fraction bounds must satisfy 0 < min < max")

    _INTEGER_FIELDS = ("fast_period", "slow_period", "atr_period", "warmup_margin_candles")
    _DECIMAL_FIELDS = (
        "min_trend_strength",
        "stop_atr_multiple",
        "trailing_atr_multiple",
        "trailing_activation_atr",
        "reward_risk_target",
        "min_atr_fraction",
        "max_atr_fraction",
        "min_stop_fraction",
        "max_stop_fraction",
    )

    @classmethod
    def scaled_for(
        cls, timeframe: Timeframe, base: Timeframe = Timeframe.M15, **overrides: Any
    ) -> EmaCrossoverParameters:
        """Move the volatility gates to another bar length by sqrt(time).

        The capital floor is not scaled. See
        ``TrendPullbackParameters.scaled_for`` for the full reasoning.
        """
        with localcontext() as arithmetic:
            arithmetic.prec = CALCULATION_PRECISION
            factor = (Decimal(timeframe.minutes) / Decimal(base.minutes)).sqrt()
        defaults = cls()
        scaled: dict[str, Any] = {
            "min_atr_fraction": defaults.min_atr_fraction * factor,
            "max_atr_fraction": defaults.max_atr_fraction * factor,
            "max_stop_fraction": defaults.max_stop_fraction * factor,
        }
        scaled.update(overrides)
        return replace(defaults, **scaled)

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> EmaCrossoverParameters:
        known = set(cls._INTEGER_FIELDS) | set(cls._DECIMAL_FIELDS)
        unknown = set(values) - known
        if unknown:
            raise StrategyError(f"Unknown strategy parameters: {sorted(unknown)}")
        kwargs: dict[str, Any] = {}
        for name in cls._INTEGER_FIELDS:
            if name in values:
                kwargs[name] = int(values[name])
        for name in cls._DECIMAL_FIELDS:
            if name in values:
                kwargs[name] = to_decimal(values[name])
        return cls(**kwargs)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {name: getattr(self, name) for name in self._INTEGER_FIELDS}
        payload.update({name: str(getattr(self, name)) for name in self._DECIMAL_FIELDS})
        return payload


class EmaCrossoverStrategy:
    """Long while the fast EMA leads the slow one by enough to matter."""

    NAME = "ema_crossover_v1"

    def __init__(self, parameters: EmaCrossoverParameters | None = None) -> None:
        self._parameters = parameters or EmaCrossoverParameters()

    @classmethod
    def from_parameters(cls, values: Mapping[str, Any]) -> EmaCrossoverStrategy:
        return cls(EmaCrossoverParameters.from_mapping(values))

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters.as_dict()

    @property
    def settings(self) -> EmaCrossoverParameters:
        return self._parameters

    @property
    def required_candles(self) -> int:
        parameters = self._parameters
        strict = max(parameters.slow_period, parameters.atr_period + 1)
        return strict + parameters.warmup_margin_candles

    def evaluate(self, context: StrategyContext) -> SignalProposal | None:
        with localcontext() as arithmetic:
            arithmetic.prec = CALCULATION_PRECISION
            return self._evaluate(context)

    def _evaluate(self, context: StrategyContext) -> SignalProposal | None:
        parameters = self._parameters
        window = context.window
        if not window.has_at_least(self.required_candles):
            return None

        closes = window.closes
        close = closes[-1]
        fast = exponential_moving_average(closes, parameters.fast_period)[-1]
        slow = exponential_moving_average(closes, parameters.slow_period)[-1]
        atr = average_true_range(window.highs, window.lows, closes, parameters.atr_period)[-1]

        if fast is None or slow is None or atr is None or atr <= ZERO or close <= ZERO:
            return None

        # ------------------------------------------------ volatility gate ---
        atr_fraction = atr / close
        if not (parameters.min_atr_fraction <= atr_fraction <= parameters.max_atr_fraction):
            return None

        # ------------------------------------------------------- the trend ---
        if fast <= slow:
            return None
        trend_strength = (fast - slow) / atr
        if trend_strength < parameters.min_trend_strength:
            # The crossover happened but the gap is inside the noise. Taking it
            # would pay a round trip for a signal the market has not made yet.
            return None

        # ------------------------------------------------------------ stop ---
        stop_price = close - parameters.stop_atr_multiple * atr
        if stop_price <= ZERO:
            return None
        stop_distance = close - stop_price
        stop_fraction = stop_distance / close
        if not (parameters.min_stop_fraction <= stop_fraction <= parameters.max_stop_fraction):
            return None

        target = close + parameters.reward_risk_target * stop_distance

        return SignalProposal(
            side=OrderSide.BUY,
            reference_price=close,
            stop_loss_price=stop_price,
            take_profit_price=target,
            confidence_score=min(trend_strength, Decimal(1)),
            score_components={"trend_strength": str(trend_strength)},
            inputs={
                "close": str(close),
                "ema_fast": str(fast),
                "ema_slow": str(slow),
                "atr": str(atr),
                "atr_fraction": str(atr_fraction),
                "trend_strength": str(trend_strength),
                "stop_distance": str(stop_distance),
                "stop_fraction": str(stop_fraction),
                # Read by the backtest engine to drive the trailing stop.
                "trailing_atr": str(parameters.trailing_atr_multiple * atr),
                "trailing_activation": str(parameters.trailing_activation_atr * atr),
                "atr_at_entry": str(atr),
            },
            candle_open_time=window.last_open_time,
        )
