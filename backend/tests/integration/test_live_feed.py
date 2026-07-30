"""Live candle ingestion against a real database.

No socket and no network: the stream and the exchange are stubs. What is
verified is the behaviour that matters when the feed misbehaves - a candle
arriving after a gap, a candle arriving twice, a candle for a symbol we never
subscribed to, and a gap repair that fails.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.config.settings import Settings
from app.core.clock import FixedClock
from app.domain.enums import CandleSource, Timeframe
from app.exchanges.base import Candle, ExchangeInfo
from app.exchanges.errors import ExchangeUnavailableError
from app.market_data.freshness import FeedFreshnessMonitor
from app.market_data.live_feed import LiveCandleFeed
from app.persistence.candles import CandleRepository
from app.persistence.models import TradingPair
from app.persistence.models.market_data import Candle as CandleRow
from app.persistence.seed import seed

pytestmark = pytest.mark.integration

M15 = Timeframe.M15
BASE = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
NOW = BASE + timedelta(days=1)


def make_candle(index: int, symbol: str = "BTCUSDT") -> Candle:
    open_time = BASE + M15.duration * index
    return Candle(
        symbol=symbol,
        timeframe=M15,
        open_time=open_time,
        close_time=open_time + M15.duration - timedelta(milliseconds=1),
        open=Decimal("65000.00"),
        high=Decimal("65250.00"),
        low=Decimal("64900.00"),
        close=Decimal("65100.50"),
        volume=Decimal("12.34567"),
        quote_volume=Decimal("803210.12345678"),
        trade_count=1543,
    )


class StubStream:
    """Replays a fixed list of closed candles."""

    def __init__(self, candles: Sequence[Candle]) -> None:
        self._candles = list(candles)

    async def closed_candles(self) -> AsyncIterator[Candle]:
        for candle in self._candles:
            yield candle


class StubAdapter:
    """Serves whatever candles it was given, filtered to the requested range."""

    def __init__(self, available: Sequence[Candle] = (), fails: bool = False) -> None:
        self._available = list(available)
        self._fails = fails
        self.calls: list[tuple[datetime | None, datetime | None]] = []

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
        self.calls.append((start, end))
        if self._fails:
            raise ExchangeUnavailableError("exchange is down")
        return [
            candle
            for candle in self._available
            if (start is None or candle.open_time >= start)
            and (end is None or candle.open_time <= end)
        ]


def session_factory(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Hands the feed sessions on the transaction the test will roll back.

    The feed commits, as it must in production. Joining by savepoint means those
    commits release a savepoint instead of the outer transaction, so the test
    still discards everything.
    """
    return async_sessionmaker(
        bind=db_connection,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )


async def build_feed(
    db_session: AsyncSession,
    db_connection: AsyncConnection,
    settings: Settings,
    stream: StubStream,
    adapter: StubAdapter,
    *,
    monitor: FeedFreshnessMonitor | None = None,
    symbol: str = "BTCUSDT",
) -> tuple[LiveCandleFeed, TradingPair]:
    await seed(db_session, settings)
    result = await db_session.execute(select(TradingPair).where(TradingPair.symbol == symbol))
    pair = result.scalar_one()

    feed = LiveCandleFeed(
        stream,
        session_factory(db_connection),
        adapter,
        {symbol: pair.id},
        clock=FixedClock(NOW),
        monitor=monitor,
    )
    return feed, pair


class TestStoringLiveCandles:
    async def test_closed_candles_are_stored_as_websocket_candles(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        stream = StubStream([make_candle(0), make_candle(1)])
        feed, pair = await build_feed(db_session, db_connection, settings, stream, StubAdapter())

        await feed.run()

        repository = CandleRepository(db_session)
        assert await repository.count(pair.id, M15) == 2
        stored = await repository.latest(pair.id, M15)
        assert stored is not None
        assert stored.source is CandleSource.WEBSOCKET
        assert feed.stats.candles_stored == 2

    async def test_a_candle_delivered_twice_is_stored_once(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        """Re-delivery after a reconnection is normal, not an error."""
        stream = StubStream([make_candle(0), make_candle(0)])
        feed, pair = await build_feed(db_session, db_connection, settings, stream, StubAdapter())

        await feed.run()

        assert await CandleRepository(db_session).count(pair.id, M15) == 1
        assert feed.stats.candles_stored == 1
        assert feed.stats.candles_already_present == 1

    async def test_a_candle_for_an_unsubscribed_symbol_is_never_stored(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        """Filing it under a guessed pair would corrupt a series we do trade."""
        stream = StubStream([make_candle(0, symbol="DOGEUSDT")])
        feed, pair = await build_feed(db_session, db_connection, settings, stream, StubAdapter())

        await feed.run()

        assert await CandleRepository(db_session).count(pair.id, M15) == 0
        assert feed.stats.candles_ignored_unknown_symbol == 1

    async def test_the_freshness_monitor_is_updated(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        monitor = FeedFreshnessMonitor(clock=FixedClock(NOW))
        stream = StubStream([make_candle(0)])
        feed, _ = await build_feed(
            db_session, db_connection, settings, stream, StubAdapter(), monitor=monitor
        )

        await feed.run()

        report = monitor.report()
        assert len(report) == 1
        assert report[0].last_candle_close_time == make_candle(0).close_time


class TestGapRepair:
    async def test_the_first_candle_ever_triggers_no_backfill(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        """Initial history is an explicit backfill, not a side effect of the
        first live candle."""
        adapter = StubAdapter()
        stream = StubStream([make_candle(10)])
        feed, _ = await build_feed(db_session, db_connection, settings, stream, adapter)

        await feed.run()

        assert adapter.calls == []
        assert feed.stats.gaps_detected == 0

    async def test_a_gap_between_candles_is_filled_from_rest(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        """The reconnection case: the socket was down for three candles."""
        missing = [make_candle(index) for index in (1, 2, 3)]
        adapter = StubAdapter(missing)
        stream = StubStream([make_candle(0), make_candle(4)])
        feed, pair = await build_feed(db_session, db_connection, settings, stream, adapter)

        await feed.run()

        repository = CandleRepository(db_session)
        assert await repository.count(pair.id, M15) == 5
        assert feed.stats.gaps_detected == 1
        assert feed.stats.candles_recovered == 3

    async def test_the_repair_requests_exactly_the_missing_range(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        adapter = StubAdapter([make_candle(index) for index in (1, 2, 3)])
        stream = StubStream([make_candle(0), make_candle(4)])
        feed, _ = await build_feed(db_session, db_connection, settings, stream, adapter)

        await feed.run()

        start, end = adapter.calls[0]
        assert start == BASE + M15.duration
        assert end == BASE + M15.duration * 3

    async def test_recovered_candles_are_marked_as_gap_fills(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        """A candle fetched after an outage was obtained under different
        conditions from one that arrived live; the row says which."""
        adapter = StubAdapter([make_candle(1)])
        stream = StubStream([make_candle(0), make_candle(2)])
        feed, pair = await build_feed(db_session, db_connection, settings, stream, adapter)

        await feed.run()

        result = await db_session.execute(
            select(CandleRow).where(
                CandleRow.trading_pair_id == pair.id,
                CandleRow.open_time == BASE + M15.duration,
            )
        )
        recovered = result.scalar_one()
        assert recovered.source is CandleSource.REST_GAP_FILL

    async def test_a_failed_repair_still_stores_the_live_candle(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        """Dropping the live candle because the repair failed would turn one
        hole into two."""
        adapter = StubAdapter(fails=True)
        stream = StubStream([make_candle(0), make_candle(4)])
        feed, pair = await build_feed(db_session, db_connection, settings, stream, adapter)

        await feed.run()

        repository = CandleRepository(db_session)
        stored = await repository.open_times(pair.id, M15, BASE, BASE + M15.duration * 10)
        assert stored == [BASE, BASE + M15.duration * 4]
        assert feed.stats.gap_repairs_failed == 1

    async def test_a_partially_repairable_gap_recovers_what_it_can(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        """The exchange may not have every candle we missed. Recovering two of
        three is better than recovering none, and the remaining hole stays
        visible."""
        adapter = StubAdapter([make_candle(1), make_candle(3)])
        stream = StubStream([make_candle(0), make_candle(4)])
        feed, pair = await build_feed(db_session, db_connection, settings, stream, adapter)

        await feed.run()

        stored = await CandleRepository(db_session).open_times(
            pair.id, M15, BASE, BASE + M15.duration * 10
        )
        assert stored == [
            BASE,
            BASE + M15.duration,
            BASE + M15.duration * 3,
            BASE + M15.duration * 4,
        ]
        assert feed.stats.candles_recovered == 2

    async def test_an_out_of_order_candle_creates_no_phantom_gap(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        """An older candle arriving late must not make the feed backfill a range
        that is already stored."""
        adapter = StubAdapter()
        stream = StubStream([make_candle(0), make_candle(1), make_candle(0)])
        feed, pair = await build_feed(db_session, db_connection, settings, stream, adapter)

        await feed.run()

        assert adapter.calls == []
        assert await CandleRepository(db_session).count(pair.id, M15) == 2


class TestIdentity:
    async def test_the_pair_mapping_is_case_insensitive(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        """Stream names are lowercase, REST symbols uppercase."""
        await seed(db_session, settings)
        result = await db_session.execute(
            select(TradingPair).where(TradingPair.symbol == "BTCUSDT")
        )
        pair = result.scalar_one()
        feed = LiveCandleFeed(
            StubStream([make_candle(0, symbol="btcusdt")]),
            session_factory(db_connection),
            StubAdapter(),
            {"btcusdt": pair.id},
            clock=FixedClock(NOW),
        )

        await feed.run()

        assert await CandleRepository(db_session).count(pair.id, M15) == 1

    async def test_a_candle_for_another_pair_writes_no_row_anywhere(
        self, db_session: AsyncSession, db_connection: AsyncConnection, settings: Settings
    ) -> None:
        """Not merely "not stored under BTCUSDT" - not stored at all."""
        feed, _ = await build_feed(
            db_session, db_connection, settings, StubStream([]), StubAdapter(), symbol="ETHUSDT"
        )
        await feed.handle(make_candle(0, symbol="BTCUSDT"))

        total = await db_session.execute(select(func.count()).select_from(CandleRow))
        assert total.scalar_one() == 0
        assert feed.stats.candles_stored == 0
        assert feed.stats.candles_ignored_unknown_symbol == 1
