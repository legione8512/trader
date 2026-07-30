"""Live candle ingestion.

Takes closed candles from the stream, stores them, and repairs whatever the
stream missed.

A WebSocket feed *will* miss candles: the connection is valid for 24 hours by
documentation, the process restarts, the network drops. None of that is
exceptional and none of it may be silent. Every stored candle is therefore
checked against the newest one already in the database, and any hole between
them is filled from REST before the new candle is stored.

The repair is what makes the difference between a series with a gap and a series
that merely arrived out of order. An indicator computed across a hole is
computed on a period that never existed.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.clock import Clock, SystemClock
from app.core.logging import get_logger
from app.domain.enums import CandleSource
from app.exchanges.base import Candle, MarketDataAdapter
from app.market_data.backfill import CandleBackfillService
from app.market_data.freshness import FeedFreshnessMonitor
from app.persistence.candles import CandleRepository

logger = get_logger(__name__)


class ClosedCandleSource(Protocol):
    """Anything that yields closed candles as they happen."""

    def closed_candles(self) -> AsyncIterator[Candle]: ...


@dataclass(slots=True)
class LiveFeedStats:
    """What the feed has done since it started. Read by tests and by logging."""

    candles_received: int = 0
    candles_stored: int = 0
    candles_already_present: int = 0
    candles_ignored_unknown_symbol: int = 0
    gaps_detected: int = 0
    candles_recovered: int = 0
    gap_repairs_failed: int = 0
    recovered_ranges: list[tuple[str, int]] = field(default_factory=list)


class LiveCandleFeed:
    """Stores closed candles arriving live and repairs gaps from REST."""

    def __init__(
        self,
        stream: ClosedCandleSource,
        session_factory: async_sessionmaker[AsyncSession],
        adapter: MarketDataAdapter,
        trading_pair_ids: Mapping[str, uuid.UUID],
        *,
        clock: Clock | None = None,
        monitor: FeedFreshnessMonitor | None = None,
    ) -> None:
        self._stream = stream
        self._session_factory = session_factory
        self._adapter = adapter
        # Uppercase keys throughout: REST symbols are uppercase, stream names are
        # lowercase, and the two meet here.
        self._pair_ids = {symbol.upper(): pair_id for symbol, pair_id in trading_pair_ids.items()}
        self._clock = clock if clock is not None else SystemClock()
        self._monitor = monitor
        self.stats = LiveFeedStats()

    async def run(self) -> None:
        """Consume the stream until the caller cancels the task."""
        async for candle in self._stream.closed_candles():
            await self.handle(candle)

    async def handle(self, candle: Candle) -> None:
        """Store one closed candle, filling any gap that precedes it."""
        self.stats.candles_received += 1

        pair_id = self._pair_ids.get(candle.symbol.upper())
        if pair_id is None:
            # Never stored under a guessed pair. A candle we cannot attribute is
            # a subscription we did not intend, and filing it anywhere would
            # corrupt a series we do trade on.
            self.stats.candles_ignored_unknown_symbol += 1
            logger.warning("live_candle_unknown_symbol", symbol=candle.symbol)
            return

        async with self._session_factory() as session:
            repository = CandleRepository(session)
            await self._repair_gap_before(session, repository, pair_id, candle)

            inserted = await repository.insert_ignoring_duplicates(
                pair_id, [candle], CandleSource.WEBSOCKET
            )
            await session.commit()

        if inserted:
            self.stats.candles_stored += 1
        else:
            # Re-delivery after a reconnection, or a candle the gap repair had
            # already fetched. Idempotent storage makes this a non-event.
            self.stats.candles_already_present += 1

        if self._monitor is not None:
            self._monitor.record_candle(candle.symbol, candle.timeframe, candle.close_time)

        logger.info(
            "live_candle_stored",
            symbol=candle.symbol,
            timeframe=candle.timeframe.value,
            open_time=candle.open_time.isoformat(),
            close=str(candle.close),
            was_new=bool(inserted),
        )

    async def _repair_gap_before(
        self,
        session: AsyncSession,
        repository: CandleRepository,
        pair_id: uuid.UUID,
        candle: Candle,
    ) -> None:
        """Fetch whatever is missing between storage and this candle.

        Nothing is done when the series is empty: the first candle ever seen has
        no predecessor to be missing, and backfilling the whole of history from
        here would be an unbounded surprise. Initial history is an explicit
        backfill, not a side effect of the first live candle.
        """
        latest = await repository.latest(pair_id, candle.timeframe)
        if latest is None:
            return

        step = candle.timeframe.duration
        first_missing = latest.open_time + step
        last_missing = candle.open_time - step
        if first_missing > last_missing:
            return

        missing_count = int((last_missing - first_missing) / step) + 1
        self.stats.gaps_detected += 1
        logger.warning(
            "live_candle_gap_detected",
            symbol=candle.symbol,
            timeframe=candle.timeframe.value,
            first_missing=first_missing.isoformat(),
            last_missing=last_missing.isoformat(),
            missing_count=missing_count,
        )

        backfill = CandleBackfillService(self._adapter, repository, clock=self._clock)
        try:
            result = await backfill.backfill(
                trading_pair_id=pair_id,
                symbol=candle.symbol,
                timeframe=candle.timeframe,
                start=first_missing,
                end=last_missing,
                source=CandleSource.REST_GAP_FILL,
            )
        except Exception as exc:
            # A failed repair must not drop the live candle. Storing it leaves a
            # visible hole that the next repair or a scheduled backfill can
            # close; discarding it would create a second one. Partial recovery
            # is rolled back rather than kept: the session may be unusable after
            # a database error, and re-fetching a range costs nothing.
            await session.rollback()
            self.stats.gap_repairs_failed += 1
            logger.error(
                "live_candle_gap_repair_failed",
                symbol=candle.symbol,
                timeframe=candle.timeframe.value,
                error=type(exc).__name__,
            )
            return

        self.stats.candles_recovered += result.candles_inserted
        self.stats.recovered_ranges.append((candle.symbol, result.candles_inserted))
        logger.info(
            "live_candle_gap_repaired",
            symbol=candle.symbol,
            timeframe=candle.timeframe.value,
            expected=missing_count,
            recovered=result.candles_inserted,
            still_missing=missing_count - result.candles_inserted,
        )
