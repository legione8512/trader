"""Downloading a long history.

The exchange is a stub - no network - so what is verified is the behaviour that
matters when a two-year download goes wrong: that a ban stops the run, that
partial work survives, and that holes are reported rather than papered over.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.clock import FixedClock
from app.domain.enums import Timeframe
from app.exchanges.base import Candle, ExchangeInfo
from app.exchanges.errors import IpBannedError
from app.market_data.history import HistoryDownloader
from app.persistence.candles import CandleRepository
from app.persistence.models import TradingPair
from app.persistence.seed import seed

pytestmark = pytest.mark.integration

M15 = Timeframe.M15
BASE = datetime(2026, 7, 1, tzinfo=UTC)
NOW = BASE + timedelta(days=30)


def candle(index: int, symbol: str) -> Candle:
    open_time = BASE + M15.duration * index
    return Candle(
        symbol=symbol,
        timeframe=M15,
        open_time=open_time,
        close_time=open_time + M15.duration - timedelta(milliseconds=1),
        open=Decimal("65000"),
        high=Decimal("65250"),
        low=Decimal("64900"),
        close=Decimal("65100"),
        volume=Decimal("12.3"),
        quote_volume=Decimal("800000"),
        trade_count=1500,
    )


class StubAdapter:
    """Serves a prepared history per symbol."""

    def __init__(
        self,
        history: dict[str, list[Candle]],
        ban_on_symbol: str | None = None,
    ) -> None:
        self._history = history
        self._ban_on_symbol = ban_on_symbol
        self.symbols_requested: list[str] = []

    async def ping(self) -> bool:
        return True

    async def server_time(self) -> datetime:
        return NOW

    async def exchange_info(self, symbols: list[str] | None = None) -> ExchangeInfo:
        raise NotImplementedError

    async def historical_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        if symbol not in self.symbols_requested:
            self.symbols_requested.append(symbol)
        if symbol == self._ban_on_symbol:
            raise IpBannedError("banned", retry_after_seconds=120.0)
        return [
            row
            for row in self._history.get(symbol, [])
            if (start is None or row.open_time >= start) and (end is None or row.open_time <= end)
        ]


async def pairs_for(db_session: AsyncSession, settings: Settings) -> list[tuple[str, uuid.UUID]]:
    await seed(db_session, settings)
    rows = (
        (await db_session.execute(select(TradingPair).order_by(TradingPair.symbol))).scalars().all()
    )
    return [(pair.symbol, pair.id) for pair in rows]


def downloader(db_session: AsyncSession, adapter: StubAdapter) -> HistoryDownloader:
    return HistoryDownloader(adapter, CandleRepository(db_session), clock=FixedClock(NOW))


class TestDownload:
    async def test_a_clean_run_stores_every_candle(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pairs = await pairs_for(db_session, settings)
        history = {symbol: [candle(index, symbol) for index in range(50)] for symbol, _ in pairs}
        report = await downloader(db_session, StubAdapter(history)).download(
            pairs=pairs, timeframe=M15, start=BASE
        )

        assert report.is_usable_for_backtesting is True
        assert report.total_inserted == 100
        for entry in report.symbols:
            assert entry.stored_total == 50
            assert entry.gaps == []

    async def test_the_stored_range_is_read_back_not_inferred(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A download that reports 70,000 candles and a table holding 69,998
        disagree about something, and the table is what a backtest reads."""
        pairs = await pairs_for(db_session, settings)
        history = {symbol: [candle(index, symbol) for index in range(20)] for symbol, _ in pairs}
        report = await downloader(db_session, StubAdapter(history)).download(
            pairs=pairs, timeframe=M15, start=BASE
        )

        entry = report.symbols[0]
        assert entry.stored_from == BASE
        assert entry.stored_to == BASE + M15.duration * 19

    async def test_rerunning_the_download_inserts_nothing_new(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pairs = await pairs_for(db_session, settings)
        history = {symbol: [candle(index, symbol) for index in range(30)] for symbol, _ in pairs}
        first = await downloader(db_session, StubAdapter(history)).download(
            pairs=pairs, timeframe=M15, start=BASE
        )
        second = await downloader(db_session, StubAdapter(history)).download(
            pairs=pairs, timeframe=M15, start=BASE
        )

        assert first.total_inserted == 60
        assert second.total_inserted == 0
        assert second.is_usable_for_backtesting is True


class TestGapsAreReportedNotHidden:
    async def test_a_hole_makes_the_history_unusable_for_backtesting(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """An indicator computed across a hole is computed over a period that
        never existed."""
        pairs = await pairs_for(db_session, settings)
        symbol = pairs[0][0]
        history = {
            symbol: [candle(index, symbol) for index in (0, 1, 2, 7, 8, 9)],
            pairs[1][0]: [candle(index, pairs[1][0]) for index in range(6)],
        }
        report = await downloader(db_session, StubAdapter(history)).download(
            pairs=pairs, timeframe=M15, start=BASE
        )

        assert report.is_usable_for_backtesting is False
        holed = next(entry for entry in report.symbols if entry.symbol == symbol)
        assert holed.gaps == [(BASE + M15.duration * 3, 4)]
        assert holed.is_complete is False

    async def test_the_other_symbols_are_still_downloaded(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A hole in one symbol is a fact about that symbol, not a reason to
        abandon the run."""
        pairs = await pairs_for(db_session, settings)
        history = {
            pairs[0][0]: [candle(index, pairs[0][0]) for index in (0, 5)],
            pairs[1][0]: [candle(index, pairs[1][0]) for index in range(6)],
        }
        report = await downloader(db_session, StubAdapter(history)).download(
            pairs=pairs, timeframe=M15, start=BASE
        )

        clean = next(entry for entry in report.symbols if entry.symbol == pairs[1][0])
        assert clean.is_complete is True
        assert clean.stored_total == 6


class TestAnIpBanStopsEverything:
    async def test_the_run_aborts_rather_than_continuing(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """The ban escalates from two minutes to three days for repeat
        offenders. Resuming later costs nothing; knocking again costs days."""
        pairs = await pairs_for(db_session, settings)
        adapter = StubAdapter({}, ban_on_symbol=pairs[0][0])
        report = await downloader(db_session, adapter).download(
            pairs=pairs, timeframe=M15, start=BASE
        )

        assert report.aborted_reason is not None
        assert "Do NOT retry" in report.aborted_reason
        assert report.is_usable_for_backtesting is False
        # The second symbol was never requested.
        assert adapter.symbols_requested == [pairs[0][0]]

    async def test_work_completed_before_the_ban_is_kept(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pairs = await pairs_for(db_session, settings)
        first_symbol, first_id = pairs[0]
        adapter = StubAdapter(
            {first_symbol: [candle(index, first_symbol) for index in range(10)]},
            ban_on_symbol=pairs[1][0],
        )
        report = await downloader(db_session, adapter).download(
            pairs=pairs, timeframe=M15, start=BASE
        )

        assert report.aborted_reason is not None
        stored = await CandleRepository(db_session).count(first_id, M15)
        assert stored == 10


class TestAlignment:
    async def test_the_start_is_aligned_to_the_interval_grid(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pairs = await pairs_for(db_session, settings)
        history = {symbol: [candle(index, symbol) for index in range(5)] for symbol, _ in pairs}
        report = await downloader(db_session, StubAdapter(history)).download(
            pairs=pairs, timeframe=M15, start=BASE + timedelta(minutes=7)
        )
        assert report.symbols[0].requested_from == BASE
