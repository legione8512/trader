"""Downloading a long history, safely.

The backfill service from Phase 3 fetches one range. This is what sits above it
when the range is two years long: it walks the symbols, reports progress, and -
most importantly - stops on an IP ban rather than working through it.

**Public data only.** Klines need no credentials, so this path cannot touch an
account even by accident. That is worth stating because a long download is
exactly the kind of job someone would be tempted to run with keys attached.

**A ban is not a retry.** Binance escalates a 418 from two minutes to three
days for repeat offenders. The service below re-raises it, this module stops
the whole run, and the operator is told which symbol got that far - because
resuming after the ban expires costs nothing, and knocking again costs days.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from app.core.clock import Clock, SystemClock
from app.core.logging import get_logger
from app.domain.candle_series import align_down, find_gaps
from app.domain.enums import CandleSource, Timeframe
from app.exchanges.base import MarketDataAdapter
from app.exchanges.errors import IpBannedError
from app.market_data.backfill import CandleBackfillService
from app.persistence.candles import CandleRepository

logger = get_logger(__name__)


@dataclass(slots=True)
class SymbolHistory:
    """What was downloaded for one symbol."""

    symbol: str
    requested_from: datetime
    requested_to: datetime
    candles_received: int = 0
    candles_inserted: int = 0
    candles_already_present: int = 0
    pages_fetched: int = 0
    stored_from: datetime | None = None
    stored_to: datetime | None = None
    stored_total: int = 0
    #: Holes remaining after the download, as (first missing, count). Reported
    #: rather than fixed silently: a hole the exchange does not have is a fact
    #: about the data, and every backtest over it should know.
    gaps: list[tuple[datetime, int]] = field(default_factory=list)
    stopped_early_reason: str | None = None

    @property
    def is_complete(self) -> bool:
        return not self.gaps and self.stopped_early_reason is None


@dataclass(slots=True)
class HistoryReport:
    """The whole run, per symbol."""

    symbols: list[SymbolHistory] = field(default_factory=list)
    aborted_reason: str | None = None

    @property
    def total_inserted(self) -> int:
        return sum(entry.candles_inserted for entry in self.symbols)

    @property
    def is_usable_for_backtesting(self) -> bool:
        """Whether every symbol came back contiguous.

        A run with holes is still worth keeping - it just must not be silently
        backtested over, because an indicator computed across a hole is computed
        over a period that never existed.
        """
        return self.aborted_reason is None and all(entry.is_complete for entry in self.symbols)


class HistoryDownloader:
    """Fetches a long range for several symbols, one at a time."""

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
        self._backfill = CandleBackfillService(adapter, repository, clock=self._clock)

    async def download(
        self,
        *,
        pairs: Sequence[tuple[str, uuid.UUID]],
        timeframe: Timeframe,
        start: datetime,
        end: datetime | None = None,
    ) -> HistoryReport:
        """Download ``timeframe`` candles for each pair, oldest first.

        Sequential on purpose. Running the symbols concurrently would multiply
        the request weight spent per minute by the number of symbols, and the
        budget is per IP, not per symbol - so concurrency here buys a modest
        speedup and risks the one error that costs days.
        """
        report = HistoryReport()
        upper = end if end is not None else self._clock.now()
        aligned_start = align_down(start, timeframe)

        for symbol, pair_id in pairs:
            entry = SymbolHistory(symbol=symbol, requested_from=aligned_start, requested_to=upper)
            report.symbols.append(entry)
            logger.info(
                "history_download_started",
                symbol=symbol,
                timeframe=timeframe.value,
                start=aligned_start.isoformat(),
                end=upper.isoformat(),
            )
            try:
                result = await self._backfill.backfill(
                    trading_pair_id=pair_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    start=aligned_start,
                    end=upper,
                    source=CandleSource.REST_BACKFILL,
                )
            except IpBannedError as error:
                # Never worked through. The ban escalates to three days for
                # repeat offenders, and resuming later costs nothing.
                report.aborted_reason = (
                    f"IP banned while downloading {symbol}. Everything fetched so far "
                    f"is stored. Do NOT retry until the ban expires "
                    f"(retry_after={error.retry_after_seconds})."
                )
                logger.error("history_download_ip_banned", symbol=symbol)
                return report

            entry.candles_received = result.candles_received
            entry.candles_inserted = result.candles_inserted
            entry.candles_already_present = result.candles_already_present
            entry.pages_fetched = result.pages_fetched
            entry.stopped_early_reason = result.stopped_early_reason

            await self._describe_stored(entry, pair_id, timeframe, aligned_start, upper)
            logger.info(
                "history_download_finished",
                symbol=symbol,
                inserted=entry.candles_inserted,
                already_present=entry.candles_already_present,
                stored_total=entry.stored_total,
                gaps=len(entry.gaps),
                complete=entry.is_complete,
            )
        return report

    async def _describe_stored(
        self,
        entry: SymbolHistory,
        pair_id: uuid.UUID,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> None:
        """Report what is actually in storage, not what the download believed.

        Read back rather than inferred. A download that reports 70,000 candles
        and a table that holds 69,998 disagree about something, and the table is
        the one a backtest will read.
        """
        open_times = await self._repository.open_times(pair_id, timeframe, start, end)
        entry.stored_total = len(open_times)
        if not open_times:
            return
        entry.stored_from = open_times[0]
        entry.stored_to = open_times[-1]
        entry.gaps = [
            (gap.first_missing_open_time, gap.missing_count)
            for gap in find_gaps(open_times, timeframe)
        ]
