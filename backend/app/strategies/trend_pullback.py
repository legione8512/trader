"""Baseline strategy: pullback within an established uptrend. Long only.

Design reviewed and approved in Phase 4.3. The reasoning, including the parts
that argue against it, is recorded here rather than in a document that can drift
away from the code.

**Long only, because the venue is spot.** Binance Spot without margin cannot
sell what it does not hold, and margin and futures permissions are forbidden by
the operator's standing instruction. The economic consequence is not a detail:
in a downtrend this strategy does not trade at all. Weeks of NO_TRADE are the
expected, correct behaviour, not a fault.

**Why a pullback rather than a breakout or a mean reversion.**

* A pure breakout wins roughly a third of the time. Rule R-06 halts the day
  after three consecutive losses, so a low win rate is structurally punished:
  the day ends before the winners arrive.
* A pure mean reversion has the win rate but not the payoff. Its reward-to-risk
  sits below 1, and at a round-trip fee of 0.200% of notional the fees eat the
  edge. Its left tail - buying into a collapse - is also exactly the risk that
  long-only spot cannot hedge.
* A pullback in a confirmed uptrend sits between them. The trend filter removes
  the falling-knife failure of mean reversion; entering on weakness gives a
  better price than a breakout, which puts the stop closer to real structure and
  buys a higher reward-to-risk at the same distance.

**Why the reward-to-risk target is 2.5 and not 2.0.** Arithmetic, not taste. The
fee is charged on notional and is therefore the same whatever the target; only
the gross edge scales with it. At a 1% stop the round trip costs 0.20R, while a
2.0 target at a 40% win rate yields a gross edge of exactly 0.20R. A 2.0 target
is break-even before anything goes wrong.

**Why stops are wide rather than tight.** Position size is risk divided by stop
distance, so a tighter stop means a larger position and a larger fee in R. A
tight stop also collides with a hard ceiling: notional cannot exceed capital
without leverage, which is forbidden, so on a 1000 RON reference with 5 RON of
risk the stop can never be tighter than 0.5% of price.

**Where it loses.** The primary failure mode is the bull trap: the pullback
resumes, then fails, for a full -1R. In a choppy uptrend that repeats, and three
of them in a row end the trading day. The strongest trends, which never pull
back, produce no entry at all - that is the price of insisting on a discount.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Any

from app.domain.enums import OrderSide
from app.domain.indicators import (
    average_true_range,
    exponential_moving_average,
    relative_strength_index,
    rolling_minimum,
)
from app.domain.money import CALCULATION_PRECISION, to_decimal
from app.strategies.base import SignalProposal, StrategyContext, StrategyError

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class TrendPullbackParameters:
    """The exact parameter set. Immutable, and stored verbatim on the version."""

    #: Regime filter. Fast above slow, and price above slow, defines "uptrend".
    trend_fast_period: int = 50
    trend_slow_period: int = 200

    #: Extra candles beyond the slow EMA's strict warm-up.
    #:
    #: Not a round number chosen for comfort. The EMA is seeded with a simple
    #: average, and that seed's weight decays as (1 - alpha)^n with
    #: alpha = 2/(period+1). For period 200 that is 0.00995 per candle, so 50
    #: extra candles still leave the seed at 60% of the value. 300 brings it
    #: under 5%, which is the point at which the indicator reflects the market
    #: rather than its own initialisation.
    warmup_margin_candles: int = 300

    #: Pullback detection.
    rsi_period: int = 14
    pullback_rsi_entry: Decimal = Decimal("40")
    pullback_lookback: int = 6

    #: Stop placement.
    atr_period: int = 14
    swing_lookback: int = 10
    #: Buffer below the swing low, in ATR. Placing the stop exactly at the swing
    #: low puts it where everyone else's is.
    stop_atr_buffer: Decimal = Decimal("0.25")

    #: Target, as a multiple of the stop distance. See the module docstring.
    reward_risk_target: Decimal = Decimal("2.5")

    #: A stop below this fraction of price would need a notional above the
    #: reference capital, which means leverage. Forbidden, so the signal is
    #: refused instead.
    min_stop_fraction: Decimal = Decimal("0.005")
    #: Above this the position becomes too small to clear the exchange's
    #: notional filter comfortably.
    max_stop_fraction: Decimal = Decimal("0.030")

    #: Volatility gate. Below the floor the fees dominate the move; above the
    #: ceiling the stop is measuring noise rather than structure.
    min_atr_fraction: Decimal = Decimal("0.0015")
    max_atr_fraction: Decimal = Decimal("0.020")

    #: Scale at which the trend-strength score component saturates: a fast EMA
    #: 1% above the slow one already counts as a strong trend.
    trend_strength_scale: Decimal = Decimal("0.01")

    def __post_init__(self) -> None:
        for name in (
            "trend_fast_period",
            "trend_slow_period",
            "rsi_period",
            "pullback_lookback",
            "atr_period",
            "swing_lookback",
        ):
            if getattr(self, name) < 1:
                raise StrategyError(f"{name} must be at least 1")
        if self.warmup_margin_candles < 0:
            raise StrategyError("warmup_margin_candles cannot be negative")
        if self.trend_fast_period >= self.trend_slow_period:
            raise StrategyError(
                f"The fast trend period must be shorter than the slow one: "
                f"{self.trend_fast_period} >= {self.trend_slow_period}"
            )
        if not (ZERO < self.pullback_rsi_entry < Decimal(100)):
            raise StrategyError("pullback_rsi_entry must be between 0 and 100 exclusive")
        if self.reward_risk_target <= ZERO:
            raise StrategyError("reward_risk_target must be positive")
        if self.stop_atr_buffer < ZERO:
            raise StrategyError("stop_atr_buffer cannot be negative")
        if self.min_stop_fraction <= ZERO or self.min_stop_fraction >= self.max_stop_fraction:
            raise StrategyError("Stop fraction bounds must satisfy 0 < min < max")
        if self.min_atr_fraction <= ZERO or self.min_atr_fraction >= self.max_atr_fraction:
            raise StrategyError("ATR fraction bounds must satisfy 0 < min < max")
        if self.trend_strength_scale <= ZERO:
            raise StrategyError("trend_strength_scale must be positive")

    # ------------------------------------------------------------ persistence ---

    _INTEGER_FIELDS = (
        "trend_fast_period",
        "trend_slow_period",
        "warmup_margin_candles",
        "rsi_period",
        "pullback_lookback",
        "atr_period",
        "swing_lookback",
    )
    _DECIMAL_FIELDS = (
        "pullback_rsi_entry",
        "stop_atr_buffer",
        "reward_risk_target",
        "min_stop_fraction",
        "max_stop_fraction",
        "min_atr_fraction",
        "max_atr_fraction",
        "trend_strength_scale",
    )

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any]) -> TrendPullbackParameters:
        """Rebuild from a stored parameter set.

        Unknown keys are refused rather than ignored. A typo silently falling
        back to the default would mean a backtest ran with parameters nobody
        chose, and the stored record would not say so.
        """
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
                # to_decimal refuses float outright, so a parameter stored as a
                # JSON number rather than a string fails loudly here instead of
                # quietly changing the arithmetic.
                kwargs[name] = to_decimal(values[name])
        return cls(**kwargs)

    def as_dict(self) -> dict[str, Any]:
        """JSON-serialisable, and complete. Decimals become strings, never floats."""
        payload: dict[str, Any] = {name: getattr(self, name) for name in self._INTEGER_FIELDS}
        payload.update({name: str(getattr(self, name)) for name in self._DECIMAL_FIELDS})
        return payload


class TrendPullbackStrategy:
    """Long-only pullback continuation. See the module docstring."""

    NAME = "trend_pullback_v1"

    def __init__(self, parameters: TrendPullbackParameters | None = None) -> None:
        self._parameters = parameters or TrendPullbackParameters()

    @classmethod
    def from_parameters(cls, values: Mapping[str, Any]) -> TrendPullbackStrategy:
        """Factory matching the registry's contract."""
        return cls(TrendPullbackParameters.from_mapping(values))

    @property
    def name(self) -> str:
        return self.NAME

    @property
    def parameters(self) -> dict[str, Any]:
        return self._parameters.as_dict()

    @property
    def settings(self) -> TrendPullbackParameters:
        """The typed parameter set, for callers that need more than JSON."""
        return self._parameters

    @property
    def required_candles(self) -> int:
        """Longest history any part of this strategy needs, plus the EMA margin.

        The RSI is checked ``pullback_lookback`` candles back, so its warm-up
        counts from there rather than from the last candle.
        """
        parameters = self._parameters
        strict = max(
            parameters.trend_slow_period,
            parameters.trend_fast_period,
            parameters.rsi_period + 1 + parameters.pullback_lookback,
            parameters.atr_period + 1,
            parameters.swing_lookback,
        )
        return strict + parameters.warmup_margin_candles

    def evaluate(self, context: StrategyContext) -> SignalProposal | None:
        """Propose a long, or ``None``.

        ``None`` is the common answer and a correct one. Which gate refused is
        deliberately not returned: reporting it would make the strategy stateful
        or widen the contract, and the same gates can be recomputed from the
        stored window when a decision is investigated.
        """
        with localcontext() as arithmetic:
            # The same working precision the indicators use. At the default 28
            # digits, `close + 2.5 * stop_distance` rounds, and a consumer
            # deriving the reward-to-risk ratio back out of the prices gets
            # 2.4999... instead of 2.5 - which a rule comparing against a
            # threshold would read as a failure.
            arithmetic.prec = CALCULATION_PRECISION
            return self._evaluate(context)

    def _evaluate(self, context: StrategyContext) -> SignalProposal | None:
        parameters = self._parameters
        window = context.window
        if not window.has_at_least(self.required_candles):
            return None

        closes = window.closes
        close = closes[-1]

        fast = exponential_moving_average(closes, parameters.trend_fast_period)[-1]
        slow = exponential_moving_average(closes, parameters.trend_slow_period)[-1]
        atr = average_true_range(window.highs, window.lows, closes, parameters.atr_period)[-1]
        swing_low = rolling_minimum(window.lows, parameters.swing_lookback)[-1]
        rsi = relative_strength_index(closes, parameters.rsi_period)

        if fast is None or slow is None or atr is None or swing_low is None:
            # Not warm despite a long enough window. Refusing beats acting on a
            # half-formed indicator, which is a different indicator.
            return None
        rsi_now = rsi[-1]
        if rsi_now is None:
            return None

        # --------------------------------------------------------- regime ---
        if fast <= slow or close <= slow:
            return None

        # ------------------------------------------------ volatility gate ---
        if close <= ZERO or atr <= ZERO:
            return None
        atr_fraction = atr / close
        if not (parameters.min_atr_fraction <= atr_fraction <= parameters.max_atr_fraction):
            return None

        # ------------------------------------------------------- pullback ---
        # The dip must be behind us, so the current candle is excluded from the
        # lookback: a dip that is still in progress is not a resumption.
        lookback = rsi[-(parameters.pullback_lookback + 1) : -1]
        deepest = min((value for value in lookback if value is not None), default=None)
        if deepest is None or deepest >= parameters.pullback_rsi_entry:
            return None

        # ------------------------------------------------------ resumption ---
        if rsi_now <= parameters.pullback_rsi_entry:
            return None
        if close <= window.highs[-2]:
            return None

        # ----------------------------------------------------------- stop ---
        stop_price = swing_low - parameters.stop_atr_buffer * atr
        if stop_price <= ZERO:
            return None
        stop_distance = close - stop_price
        if stop_distance <= ZERO:
            return None
        stop_fraction = stop_distance / close
        if not (parameters.min_stop_fraction <= stop_fraction <= parameters.max_stop_fraction):
            # Too tight would need leverage; too wide leaves a position too
            # small to clear the exchange's notional filter.
            return None

        target_price = close + parameters.reward_risk_target * stop_distance

        # Prices are NOT rounded to the exchange tick here. The strategy cannot
        # see the symbol filters by design, and rounding to a guessed tick would
        # move the stop - and therefore the risk - by an unknown amount.
        components = self._score_components(fast, slow, deepest)
        return SignalProposal(
            side=OrderSide.BUY,
            reference_price=close,
            stop_loss_price=stop_price,
            take_profit_price=target_price,
            confidence_score=sum(components.values(), ZERO) / Decimal(len(components)),
            score_components={name: str(value) for name, value in components.items()},
            inputs={
                "close": str(close),
                "ema_fast": str(fast),
                "ema_slow": str(slow),
                "rsi": str(rsi_now),
                "rsi_pullback_low": str(deepest),
                "atr": str(atr),
                "atr_fraction": str(atr_fraction),
                "swing_low": str(swing_low),
                "stop_distance": str(stop_distance),
                "stop_fraction": str(stop_fraction),
            },
            candle_open_time=window.last_open_time,
        )

    def _score_components(
        self, fast: Decimal, slow: Decimal, deepest_rsi: Decimal
    ) -> dict[str, Decimal]:
        """Components of an internal RANKING score, in [0, 1].

        **Not a probability.** It has never been calibrated against realised
        outcomes, and presenting it as one would be a claim nobody has earned.
        Its only job is to choose between simultaneous proposals when
        ``maximumOpenPositions`` is 1 and both symbols qualify at once.
        """
        parameters = self._parameters
        trend_strength = ZERO
        if slow > ZERO:
            trend_strength = _clamp((fast / slow - ONE) / parameters.trend_strength_scale)
        pullback_depth = _clamp(
            (parameters.pullback_rsi_entry - deepest_rsi) / parameters.pullback_rsi_entry
        )
        return {"trend_strength": trend_strength, "pullback_depth": pullback_depth}


def _clamp(value: Decimal, lower: Decimal = ZERO, upper: Decimal = ONE) -> Decimal:
    return max(lower, min(upper, value))
