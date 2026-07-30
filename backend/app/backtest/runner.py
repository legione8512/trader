"""Running a backtest over stored candles.

Two things here that are not arithmetic.

**The window is refused if it has a hole.** ``CandleWindow`` enforces that, and
this module does not work around it. A run over a series with a gap produces
indicator values computed across a period that never existed, and no amount of
care further down recovers from that.

**The out-of-sample split is by time, and the split point is chosen before any
result is seen.** That is the whole point of the exercise. A split moved after
looking at the numbers is not out-of-sample testing, it is choosing the answer.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.backtest.engine import BacktestConfig, BacktestResult, run_backtest
from app.backtest.metrics import BacktestMetrics, compute_metrics
from app.domain.candle_window import CandleWindow
from app.domain.enums import Timeframe
from app.domain.errors import DomainError
from app.persistence.candles import CandleRepository
from app.strategies.base import Strategy

#: Fraction of the history reserved for the final, single out-of-sample check.
#: Fixed here rather than passed in, because a split anyone can move is a split
#: someone will move.
OUT_OF_SAMPLE_FRACTION = Decimal("0.30")


class BacktestRunnerError(DomainError):
    """The run cannot proceed on the data available."""


@dataclass(frozen=True, slots=True)
class Segment:
    """One run over one stretch of history."""

    label: str
    first_open_time: datetime
    last_open_time: datetime
    candle_count: int
    result: BacktestResult
    metrics: BacktestMetrics


async def load_window(
    session: AsyncSession,
    *,
    trading_pair_id: uuid.UUID,
    symbol: str,
    timeframe: Timeframe,
    start: datetime,
    end: datetime,
) -> CandleWindow:
    """Read stored candles into a window, refusing anything discontinuous."""
    repository = CandleRepository(session)
    rows = await repository.range(trading_pair_id, timeframe, start, end)
    if not rows:
        raise BacktestRunnerError(
            f"No stored candles for {symbol} {timeframe.value} in the requested range. "
            f"Run: python -m app.cli backfill"
        )
    return CandleWindow(
        symbol=symbol,
        timeframe=timeframe,
        open_times=tuple(row.open_time for row in rows),
        opens=tuple(row.open_price for row in rows),
        highs=tuple(row.high_price for row in rows),
        lows=tuple(row.low_price for row in rows),
        closes=tuple(row.close_price for row in rows),
        volumes=tuple(row.volume for row in rows),
    )


def split_in_sample(window: CandleWindow) -> tuple[CandleWindow, CandleWindow]:
    """Split by time: development history first, held-out history last.

    Chronological, never random. A random split would let the strategy be tuned
    on candles that came after the ones it is tested on, which is look-ahead
    bias wearing the clothes of good statistical practice.
    """
    total = len(window)
    held_out = int(Decimal(total) * OUT_OF_SAMPLE_FRACTION)
    boundary = total - held_out
    if boundary <= 0 or held_out <= 0:
        raise BacktestRunnerError(
            f"{total} candles is too few to split into development and held-out sets."
        )
    return window.slice(0, boundary), window.slice(boundary, total)


def run_segment(
    strategy: Strategy, window: CandleWindow, config: BacktestConfig, label: str
) -> Segment:
    result = run_backtest(strategy, window, config)
    return Segment(
        label=label,
        first_open_time=window.open_times[0],
        last_open_time=window.open_times[-1],
        candle_count=len(window),
        result=result,
        metrics=compute_metrics(result),
    )


def format_segment(segment: Segment) -> str:
    """A report that leads with what the numbers cannot support."""
    result = segment.result
    metrics = segment.metrics
    lines = [
        f"=== {segment.label} ===",
        f"Period: {segment.first_open_time.date()} to {segment.last_open_time.date()} "
        f"({segment.candle_count} candles)",
        f"Proposals: {result.proposals}   Entries unfilled: {result.entries_expired_unfilled}",
        "",
        metrics.summary(),
    ]
    if result.rejections:
        lines.append("")
        lines.append("Refused by the risk engine:")
        lines.extend(
            f"  {code}: {count}"
            for code, count in sorted(result.rejections.items(), key=lambda item: -item[1])
        )
    if metrics.by_exit_trigger:
        lines.append("")
        lines.append("Exits:")
        lines.extend(
            f"  {label}: {entry.trade_count} trades, {entry.total_r} R"
            for label, entry in metrics.by_exit_trigger.items()
        )
    return "\n".join(lines)
