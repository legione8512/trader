"""Historical candle backfill.

Paging the klines endpoint and storing what comes back, with two rules that are
easy to get wrong and expensive to get wrong:

1. **The in-progress candle is discarded.** The REST endpoint returns it as the
   last element when the requested range reaches the present, and it repaints
   until it closes. Storing it would let a backtest see values that were not
   visible at decision time.
2. **A rate limit is respected, an IP ban is escalated.** 429 waits for
   ``Retry-After``; 418 stops the backfill and raises, because knocking again
   extends a ban that already lasts up to three days.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from app.core.clock import Clock, SystemClock
from app.core.logging import get_logger
from app.domain.candle_series import (
    CandleGap,
    align_down,
    find_gaps,
    is_closed_at,
    validate_ordering,
)
from app.domain.enums import CandleSource, Timeframe
from app.exchanges.base import Candle as CandleValue
from app.exchanges.base import MarketDataAdapter
from app.exchanges.binance.constants import KLINES_MAX_LIMIT
from app.exchanges.binance.rest import sleep_for_retry_after
from app.exchanges.errors import IpBannedError, RateLimitError
from app.persistence.candles import CandleRepository

logger = get_logger(__name__)

#: How many pages one backfill will request before giving up. A guard against a
#: pagination bug turning into an unbounded request loop against the exchange.
MAX_PAGES = 500


@dataclass(slots=True)
class BackfillResult:
    """What a backfill actually did, reported honestly."""

    symbol: str
    timeframe: Timeframe
    pages_fetched: int = 0
    candles_received: int = 0
    candles_discarded_unclosed: int = 0
    candles_inserted: int = 0
    gaps: list[CandleGap] = field(default_factory=list)
    stopped_early_reason: str | None = None

    @property
    def candles_already_present(self) -> int:
        return self.candles_received - self.candles_discarded_unclosed - self.candles_inserted

    @property
    def is_contiguous(self) -> bool:
        return not self.gaps


class CandleBackfillService:
    """Fetches and stores a historical range of candles."""

    def __init__(
        self,
        adapter: MarketDataAdapter,
        repository: CandleRepository,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._adapter = adapter
        self._repository = repository
        self._clock = clock if clock is not None else SystemClock()

    async def backfill(
        self,
        *,
        trading_pair_id: uuid.UUID,
        symbol: str,
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
        source: CandleSource = CandleSource.REST_BACKFILL,
        page_size: int = KLINES_MAX_LIMIT,
    ) -> BackfillResult:
        """Fetch closed candles from ``start`` and store them.

        Safe to re-run: storage skips candles already present, so a backfill
        interrupted halfway simply resumes.
        """
        now = self._clock.now()
        upper_bound = end if end is not None else now
        result = BackfillResult(symbol=symbol, timeframe=timeframe)

        cursor = align_down(start, timeframe)
        stored_open_times: list[datetime] = []

        for _ in range(MAX_PAGES):
            if cursor > upper_bound:
                break

            try:
                page = await self._adapter.historical_candles(
                    symbol, timeframe, start=cursor, end=upper_bound, limit=page_size
                )
            except IpBannedError:
                # Never waited out. It stops the backfill and reaches the
                # operator, because retrying extends the ban.
                result.stopped_early_reason = "IP_BANNED"
                raise
            except RateLimitError as error:
                logger.warning(
                    "backfill_rate_limited",
                    symbol=symbol,
                    retry_after_seconds=error.retry_after_seconds,
                )
                await sleep_for_retry_after(error)
                continue

            result.pages_fetched += 1
            result.candles_received += len(page)

            if not page:
                break

            closed = self._only_closed(page, now, result)
            if closed:
                inserted = await self._repository.insert_ignoring_duplicates(
                    trading_pair_id, closed, source
                )
                result.candles_inserted += inserted
                stored_open_times.extend(candle.open_time for candle in closed)

            # Advance past the last candle we actually saw, closed or not.
            # Using the last RECEIVED candle rather than the last stored one
            # matters: if the only thing on this page was the in-progress
            # candle, using the stored list would leave the cursor unmoved and
            # loop forever.
            last_open_time = page[-1].open_time
            next_cursor = last_open_time + timeframe.duration
            if next_cursor <= cursor:
                result.stopped_early_reason = "CURSOR_DID_NOT_ADVANCE"
                break
            cursor = next_cursor

            if len(page) < page_size:
                # A short page means the exchange has nothing more in range.
                break
        else:
            result.stopped_early_reason = "MAX_PAGES_REACHED"

        if stored_open_times:
            validate_ordering(stored_open_times, timeframe)
            result.gaps = find_gaps(stored_open_times, timeframe)

        logger.info(
            "backfill_completed",
            symbol=symbol,
            timeframe=timeframe.value,
            pages=result.pages_fetched,
            received=result.candles_received,
            inserted=result.candles_inserted,
            discarded_unclosed=result.candles_discarded_unclosed,
            gaps=len(result.gaps),
            stopped_early_reason=result.stopped_early_reason,
        )
        return result

    @staticmethod
    def _only_closed(
        page: list[CandleValue], now: datetime, result: BackfillResult
    ) -> list[CandleValue]:
        """Drop the in-progress candle, if the page contains one."""
        closed: list[CandleValue] = []
        for candle in page:
            if is_closed_at(candle.close_time, now):
                closed.append(candle)
            else:
                result.candles_discarded_unclosed += 1
        return closed
