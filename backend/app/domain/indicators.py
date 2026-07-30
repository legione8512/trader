"""Technical indicators.

Pure functions over sequences of ``Decimal``. No candles, no clock, no I/O: an
indicator does not need to know what a candle is, it needs numbers.

Three rules shape every function here.

**1. The result is aligned to the input, with ``None`` during warm-up.**
Never a bare number. A bare number hides how many observations it needed and
invites using an indicator before it has one - the classic way a backtest ends
up trading on a value that did not exist yet. ``result[i]`` is the indicator as
it stood *after* observation ``i``, or ``None`` if there was not enough history.

**2. ``result[i]`` depends only on ``values[0..i]``.** This is look-ahead bias
made structurally impossible rather than merely avoided, and it is verified by a
property test: computing over a truncated series must give identical values for
the overlapping prefix.

**3. The smoothing variant is named, not implied.** "ATR" and "RSI" are
ambiguous in the wild - Wilder's smoothing, a simple average and an exponential
average give different numbers from the same data. A strategy calibrated against
one and executed against another is calibrated against nothing. Every function
here states which one it is, and Wilder's is spelled ``wilder_``.

Arithmetic runs at ``CALCULATION_PRECISION`` inside an explicit context, so the
same inputs produce the same digits on every machine - which is what makes a
backtest reproducible (AC-20).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, localcontext

from app.domain.errors import DomainError
from app.domain.money import CALCULATION_PRECISION

#: A series of observations, oldest first.
Series = Sequence[Decimal]
#: An indicator series, aligned to its input. ``None`` marks warm-up.
IndicatorSeries = list[Decimal | None]

ZERO = Decimal(0)
ONE = Decimal(1)
HUNDRED = Decimal(100)


class IndicatorError(DomainError):
    """The indicator cannot be computed from what it was given."""


def _require_period(period: int, *, minimum: int = 1) -> None:
    if period < minimum:
        raise IndicatorError(f"Period must be at least {minimum}, got {period}")


def _require_aligned(name: str, first: Series, *rest: Series) -> None:
    for other in rest:
        if len(other) != len(first):
            raise IndicatorError(
                f"{name} needs series of equal length: got {len(first)} and {len(other)}"
            )


# ---------------------------------------------------------------- averages ---


def simple_moving_average(values: Series, period: int) -> IndicatorSeries:
    """Unweighted mean of the last ``period`` observations.

    Warm-up: ``period - 1`` observations. The first value lands at index
    ``period - 1``.
    """
    _require_period(period)
    result: IndicatorSeries = [None] * len(values)
    if len(values) < period:
        return result

    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        window_total = sum(values[:period], ZERO)
        divisor = Decimal(period)
        result[period - 1] = window_total / divisor
        for index in range(period, len(values)):
            # Rolling update rather than re-summing: exact for Decimal, since
            # addition and subtraction of Decimals are exact at this precision.
            window_total += values[index] - values[index - period]
            result[index] = window_total / divisor
    return result


def exponential_moving_average(values: Series, period: int) -> IndicatorSeries:
    """EMA with the standard ``2 / (period + 1)`` smoothing factor.

    Seeded with the simple average of the first ``period`` observations. The
    seeding matters and is stated on purpose: seeding with the first value
    instead produces a different series for hundreds of bars, and two systems
    that "both use the EMA" would disagree.
    """
    _require_period(period)
    result: IndicatorSeries = [None] * len(values)
    if len(values) < period:
        return result

    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        multiplier = Decimal(2) / Decimal(period + 1)
        previous = sum(values[:period], ZERO) / Decimal(period)
        result[period - 1] = previous
        for index in range(period, len(values)):
            previous = (values[index] - previous) * multiplier + previous
            result[index] = previous
    return result


def wilder_moving_average(values: Series, period: int) -> IndicatorSeries:
    """Wilder's smoothing (RMA), the basis of his ATR and RSI.

    ``next = previous + (value - previous) / period``. Equivalent to an EMA of
    period ``2 * period - 1``, which is why an "ATR(14)" computed with a plain
    EMA(14) is noticeably faster-moving than Wilder's.
    """
    _require_period(period)
    result: IndicatorSeries = [None] * len(values)
    if len(values) < period:
        return result

    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        divisor = Decimal(period)
        previous = sum(values[:period], ZERO) / divisor
        result[period - 1] = previous
        for index in range(period, len(values)):
            previous = previous + (values[index] - previous) / divisor
            result[index] = previous
    return result


# ------------------------------------------------------------- volatility ---


def true_range(highs: Series, lows: Series, closes: Series) -> IndicatorSeries:
    """True range: the greatest of high-low, |high-previous close| and
    |low-previous close|.

    Index 0 is ``None``, not ``high - low``. Without a previous close the true
    range is not the true range - it is the bar range, which ignores exactly the
    overnight gap the indicator exists to capture. Returning ``None`` costs one
    observation of warm-up and keeps the definition honest.
    """
    _require_aligned("true_range", highs, lows, closes)
    result: IndicatorSeries = [None] * len(highs)
    for index in range(1, len(highs)):
        previous_close = closes[index - 1]
        result[index] = max(
            highs[index] - lows[index],
            abs(highs[index] - previous_close),
            abs(lows[index] - previous_close),
        )
    return result


def average_true_range(highs: Series, lows: Series, closes: Series, period: int) -> IndicatorSeries:
    """ATR using **Wilder's** smoothing, as originally defined.

    Warm-up: ``period`` observations, one more than a plain average would need,
    because the first true range is undefined. ATR(14) therefore needs 15
    candles before it produces anything.
    """
    _require_period(period)
    _require_aligned("average_true_range", highs, lows, closes)
    if not highs:
        return []

    ranges = true_range(highs, lows, closes)
    # Index 0 is None by definition; smooth the defined tail and shift back.
    defined = [value for value in ranges[1:] if value is not None]
    smoothed = wilder_moving_average(defined, period)
    return [None, *smoothed]


# --------------------------------------------------------------- momentum ---


def relative_strength_index(closes: Series, period: int) -> IndicatorSeries:
    """RSI using **Wilder's** smoothing of average gain and average loss.

    Warm-up: ``period`` observations. The first value lands at index ``period``,
    because the first change requires two closes.

    The degenerate cases are handled explicitly rather than by division, because
    dividing by a zero average loss would raise and clamping it to a small
    number would invent a value nobody can explain:

    * gains but no losses -> 100 (maximum strength);
    * losses but no gains -> 0;
    * neither -> 50. A perfectly flat window has no directional pressure in
      either direction, so the neutral reading is the honest one.
    """
    _require_period(period)
    result: IndicatorSeries = [None] * len(closes)
    if len(closes) <= period:
        return result

    with localcontext() as context:
        context.prec = CALCULATION_PRECISION
        divisor = Decimal(period)

        gains: list[Decimal] = []
        losses: list[Decimal] = []
        for index in range(1, len(closes)):
            change = closes[index] - closes[index - 1]
            gains.append(change if change > ZERO else ZERO)
            losses.append(-change if change < ZERO else ZERO)

        average_gain = sum(gains[:period], ZERO) / divisor
        average_loss = sum(losses[:period], ZERO) / divisor
        result[period] = _rsi_from(average_gain, average_loss)

        for offset in range(period, len(gains)):
            average_gain += (gains[offset] - average_gain) / divisor
            average_loss += (losses[offset] - average_loss) / divisor
            result[offset + 1] = _rsi_from(average_gain, average_loss)
    return result


def _rsi_from(average_gain: Decimal, average_loss: Decimal) -> Decimal:
    if average_loss == ZERO:
        # No losses in the window: maximum strength. Not "undefined", and not a
        # division that would raise.
        return HUNDRED if average_gain > ZERO else Decimal(50)
    if average_gain == ZERO:
        return ZERO
    relative_strength = average_gain / average_loss
    return HUNDRED - (HUNDRED / (ONE + relative_strength))


# ---------------------------------------------------------------- extremes ---


def rolling_maximum(values: Series, period: int) -> IndicatorSeries:
    """Highest value over the last ``period`` observations, inclusive."""
    _require_period(period)
    result: IndicatorSeries = [None] * len(values)
    for index in range(period - 1, len(values)):
        result[index] = max(values[index - period + 1 : index + 1])
    return result


def rolling_minimum(values: Series, period: int) -> IndicatorSeries:
    """Lowest value over the last ``period`` observations, inclusive."""
    _require_period(period)
    result: IndicatorSeries = [None] * len(values)
    for index in range(period - 1, len(values)):
        result[index] = min(values[index - period + 1 : index + 1])
    return result


# ----------------------------------------------------------------- helpers ---


def warmup_length(series: IndicatorSeries) -> int:
    """How many leading observations produced no value.

    Used to refuse a decision on a strategy that is not warm yet, rather than
    letting it act on the first value it happens to get.
    """
    for index, value in enumerate(series):
        if value is not None:
            return index
    return len(series)


def latest(series: IndicatorSeries) -> Decimal | None:
    """The most recent value, or ``None`` if the indicator is not warm."""
    return series[-1] if series else None


def is_warm(series: IndicatorSeries) -> bool:
    """Whether the indicator has a value for its most recent observation."""
    return bool(series) and series[-1] is not None
