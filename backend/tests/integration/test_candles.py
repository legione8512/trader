"""Candle persistence and backfill, against a real database.

The exchange is a stub: no network is touched. What is verified here is
storage idempotency, the constraints, and that the backfill pages correctly and
refuses to store an in-progress candle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.clock import FixedClock
from app.domain.enums import CandleSource, Timeframe
from app.exchanges.base import Candle as CandleValue
from app.exchanges.base import ExchangeInfo
from app.exchanges.errors import IpBannedError, RateLimitError
from app.market_data.backfill import CandleBackfillService
from app.persistence.candles import CandleRepository
from app.persistence.models import TradingPair
from app.persistence.models.market_data import Candle
from app.persistence.seed import seed

pytestmark = pytest.mark.integration

M15 = Timeframe.M15
BASE = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
#: Far enough after the series that every generated candle has closed.
NOW = BASE + timedelta(days=2)


def make_candle(index: int, symbol: str = "BTCUSDT") -> CandleValue:
    open_time = BASE + M15.duration * index
    return CandleValue(
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


async def get_pair(db_session: AsyncSession, settings: Settings) -> TradingPair:
    await seed(db_session, settings)
    result = await db_session.execute(select(TradingPair).where(TradingPair.symbol == "BTCUSDT"))
    return result.scalar_one()


class StubAdapter:
    """A market data adapter that serves prepared pages."""

    def __init__(self, pages: list[list[CandleValue]]) -> None:
        self._pages = pages
        self.calls: list[dict[str, object]] = []
        self.raise_on_call: dict[int, Exception] = {}

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
    ) -> list[CandleValue]:
        index = len(self.calls)
        self.calls.append({"symbol": symbol, "start": start, "limit": limit})
        if index in self.raise_on_call:
            raise self.raise_on_call[index]
        return self._pages[index] if index < len(self._pages) else []


class TestCandleStorage:
    async def test_candles_survive_a_round_trip_as_exact_decimals(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        repository = CandleRepository(db_session)

        await repository.insert_ignoring_duplicates(
            pair.id, [make_candle(0)], CandleSource.REST_BACKFILL
        )
        db_session.expunge_all()

        stored = await repository.latest(pair.id, M15)
        assert stored is not None
        assert stored.open_price == Decimal("65000.00")
        assert stored.quote_volume == Decimal("803210.12345678")
        assert stored.trade_count == 1543
        assert stored.source is CandleSource.REST_BACKFILL

    async def test_reinserting_the_same_candles_changes_nothing(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """What makes a backfill safe to re-run after a restart."""
        pair = await get_pair(db_session, settings)
        repository = CandleRepository(db_session)
        candles = [make_candle(index) for index in range(5)]

        first = await repository.insert_ignoring_duplicates(
            pair.id, candles, CandleSource.REST_BACKFILL
        )
        second = await repository.insert_ignoring_duplicates(
            pair.id, candles, CandleSource.REST_BACKFILL
        )

        assert first == 5
        assert second == 0
        assert await repository.count(pair.id, M15) == 5

    async def test_a_partially_overlapping_batch_inserts_only_what_is_new(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        repository = CandleRepository(db_session)

        await repository.insert_ignoring_duplicates(
            pair.id, [make_candle(i) for i in range(3)], CandleSource.REST_BACKFILL
        )
        inserted = await repository.insert_ignoring_duplicates(
            pair.id, [make_candle(i) for i in range(1, 6)], CandleSource.REST_GAP_FILL
        )

        assert inserted == 3
        assert await repository.count(pair.id, M15) == 6

    async def test_the_same_pair_timeframe_and_open_time_cannot_exist_twice(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """A candle has no identity apart from its coordinates."""
        pair = await get_pair(db_session, settings)
        values = {
            "trading_pair_id": pair.id,
            "timeframe": M15,
            "open_time": BASE,
            "close_time": BASE + M15.duration,
            "open_price": Decimal("1"),
            "high_price": Decimal("1"),
            "low_price": Decimal("1"),
            "close_price": Decimal("1"),
            "volume": Decimal("1"),
            "quote_volume": Decimal("1"),
            "trade_count": 1,
            "source": CandleSource.WEBSOCKET,
        }
        db_session.add(Candle(**values))
        await db_session.flush()

        db_session.add(Candle(**values))
        with pytest.raises(IntegrityError):
            await db_session.flush()

    async def test_the_same_open_time_on_another_timeframe_is_a_different_candle(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        for timeframe in (Timeframe.M15, Timeframe.H1):
            db_session.add(
                Candle(
                    trading_pair_id=pair.id,
                    timeframe=timeframe,
                    open_time=BASE,
                    close_time=BASE + timeframe.duration,
                    open_price=Decimal("1"),
                    high_price=Decimal("1"),
                    low_price=Decimal("1"),
                    close_price=Decimal("1"),
                    volume=Decimal("1"),
                    quote_volume=Decimal("1"),
                    trade_count=1,
                    source=CandleSource.REST_BACKFILL,
                )
            )
        await db_session.flush()

        repository = CandleRepository(db_session)
        assert await repository.count(pair.id, Timeframe.M15) == 1
        assert await repository.count(pair.id, Timeframe.H1) == 1


class TestCandleConstraints:
    async def _insert(
        self, db_session: AsyncSession, pair: TradingPair, **overrides: object
    ) -> None:
        values: dict[str, object] = {
            "trading_pair_id": pair.id,
            "timeframe": M15,
            "open_time": BASE,
            "close_time": BASE + M15.duration,
            "open_price": Decimal("65000"),
            "high_price": Decimal("65250"),
            "low_price": Decimal("64900"),
            "close_price": Decimal("65100"),
            "volume": Decimal("1"),
            "quote_volume": Decimal("1"),
            "trade_count": 1,
            "source": CandleSource.REST_BACKFILL,
        }
        values.update(overrides)
        db_session.add(Candle(**values))
        await db_session.flush()

    async def test_a_high_below_the_low_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        with pytest.raises(IntegrityError):
            await self._insert(db_session, pair, high_price=Decimal("64000"))

    async def test_a_close_outside_the_range_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Storing one would poison every indicator computed over it."""
        pair = await get_pair(db_session, settings)
        with pytest.raises(IntegrityError):
            await self._insert(db_session, pair, close_price=Decimal("70000"))

    async def test_a_candle_closing_before_it_opens_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        with pytest.raises(IntegrityError):
            await self._insert(db_session, pair, close_time=BASE - timedelta(minutes=1))

    async def test_a_negative_volume_is_refused(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        with pytest.raises(IntegrityError):
            await self._insert(db_session, pair, volume=Decimal("-1"))


class TestQueries:
    async def test_the_latest_candle_is_the_newest_one(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        repository = CandleRepository(db_session)
        await repository.insert_ignoring_duplicates(
            pair.id, [make_candle(i) for i in range(10)], CandleSource.REST_BACKFILL
        )

        latest = await repository.latest(pair.id, M15)
        earliest = await repository.earliest(pair.id, M15)
        assert latest is not None and earliest is not None
        assert latest.open_time == BASE + M15.duration * 9
        assert earliest.open_time == BASE

    async def test_a_range_query_includes_both_bounds(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        repository = CandleRepository(db_session)
        await repository.insert_ignoring_duplicates(
            pair.id, [make_candle(i) for i in range(10)], CandleSource.REST_BACKFILL
        )

        found = await repository.range(
            pair.id, M15, BASE + M15.duration * 2, BASE + M15.duration * 5
        )
        assert len(found) == 4
        assert found[0].open_time == BASE + M15.duration * 2

    async def test_open_times_come_back_ordered_for_gap_detection(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        repository = CandleRepository(db_session)
        # Deliberately insert out of order; storage must not care.
        await repository.insert_ignoring_duplicates(
            pair.id, [make_candle(i) for i in (3, 0, 2)], CandleSource.REST_BACKFILL
        )

        times = await repository.open_times(pair.id, M15, BASE, BASE + M15.duration * 10)
        assert times == [BASE, BASE + M15.duration * 2, BASE + M15.duration * 3]


class TestBackfill:
    async def test_a_single_page_is_fetched_and_stored(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        adapter = StubAdapter([[make_candle(i) for i in range(5)]])
        service = CandleBackfillService(
            adapter, CandleRepository(db_session), clock=FixedClock(NOW)
        )

        result = await service.backfill(
            trading_pair_id=pair.id, symbol="BTCUSDT", timeframe=M15, start=BASE, page_size=100
        )

        assert result.candles_received == 5
        assert result.candles_inserted == 5
        assert result.is_contiguous is True

    async def test_full_pages_are_followed_until_a_short_one(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        adapter = StubAdapter(
            [
                [make_candle(i) for i in range(0, 3)],
                [make_candle(i) for i in range(3, 6)],
                [make_candle(6)],
            ]
        )
        service = CandleBackfillService(
            adapter, CandleRepository(db_session), clock=FixedClock(NOW)
        )

        result = await service.backfill(
            trading_pair_id=pair.id, symbol="BTCUSDT", timeframe=M15, start=BASE, page_size=3
        )

        assert result.pages_fetched == 3
        assert result.candles_inserted == 7
        assert await CandleRepository(db_session).count(pair.id, M15) == 7

    async def test_the_cursor_advances_past_the_last_candle_of_each_page(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        adapter = StubAdapter(
            [[make_candle(i) for i in range(0, 3)], [make_candle(i) for i in range(3, 5)]]
        )
        service = CandleBackfillService(
            adapter, CandleRepository(db_session), clock=FixedClock(NOW)
        )

        await service.backfill(
            trading_pair_id=pair.id, symbol="BTCUSDT", timeframe=M15, start=BASE, page_size=3
        )

        assert adapter.calls[0]["start"] == BASE
        assert adapter.calls[1]["start"] == BASE + M15.duration * 3

    async def test_the_in_progress_candle_is_never_stored(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """The whole reason this filter exists: it would be look-ahead bias."""
        pair = await get_pair(db_session, settings)
        # Four closed candles plus the one currently forming.
        closed = [make_candle(i) for i in range(4)]
        forming = make_candle(4)
        now = forming.open_time + timedelta(minutes=3)

        adapter = StubAdapter([[*closed, forming]])
        service = CandleBackfillService(
            adapter, CandleRepository(db_session), clock=FixedClock(now)
        )

        result = await service.backfill(
            trading_pair_id=pair.id, symbol="BTCUSDT", timeframe=M15, start=BASE, page_size=100
        )

        assert result.candles_received == 5
        assert result.candles_discarded_unclosed == 1
        assert result.candles_inserted == 4

        stored = await CandleRepository(db_session).latest(pair.id, M15)
        assert stored is not None
        assert stored.open_time == closed[-1].open_time

    async def test_a_page_holding_only_the_forming_candle_terminates(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """The cursor must advance on the RECEIVED candle, or this loops forever."""
        pair = await get_pair(db_session, settings)
        forming = make_candle(0)
        now = forming.open_time + timedelta(minutes=3)

        adapter = StubAdapter([[forming]])
        service = CandleBackfillService(
            adapter, CandleRepository(db_session), clock=FixedClock(now)
        )

        result = await service.backfill(
            trading_pair_id=pair.id, symbol="BTCUSDT", timeframe=M15, start=BASE, page_size=100
        )

        assert result.candles_inserted == 0
        assert result.candles_discarded_unclosed == 1
        assert result.stopped_early_reason is None

    async def test_a_gap_in_what_was_stored_is_reported(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        adapter = StubAdapter([[make_candle(i) for i in (0, 1, 4, 5)]])
        service = CandleBackfillService(
            adapter, CandleRepository(db_session), clock=FixedClock(NOW)
        )

        result = await service.backfill(
            trading_pair_id=pair.id, symbol="BTCUSDT", timeframe=M15, start=BASE, page_size=100
        )

        assert result.is_contiguous is False
        assert len(result.gaps) == 1
        assert result.gaps[0].missing_count == 2

    async def test_rerunning_a_backfill_inserts_nothing_new(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        repository = CandleRepository(db_session)
        page = [make_candle(i) for i in range(5)]

        first = await CandleBackfillService(
            StubAdapter([page]), repository, clock=FixedClock(NOW)
        ).backfill(
            trading_pair_id=pair.id, symbol="BTCUSDT", timeframe=M15, start=BASE, page_size=100
        )
        second = await CandleBackfillService(
            StubAdapter([page]), repository, clock=FixedClock(NOW)
        ).backfill(
            trading_pair_id=pair.id, symbol="BTCUSDT", timeframe=M15, start=BASE, page_size=100
        )

        assert first.candles_inserted == 5
        assert second.candles_inserted == 0
        assert second.candles_already_present == 5

    async def test_a_rate_limit_is_waited_out_and_the_page_retried(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        adapter = StubAdapter([[], [make_candle(i) for i in range(3)]])
        adapter.raise_on_call = {0: RateLimitError("slow down", retry_after_seconds=0.0)}
        service = CandleBackfillService(
            adapter, CandleRepository(db_session), clock=FixedClock(NOW)
        )

        result = await service.backfill(
            trading_pair_id=pair.id, symbol="BTCUSDT", timeframe=M15, start=BASE, page_size=100
        )

        # The refused call did not count as a page, and the retry did.
        assert result.candles_inserted == 3
        assert len(adapter.calls) == 2

    async def test_an_ip_ban_stops_the_backfill_rather_than_waiting(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        """Retrying extends a ban that already lasts up to three days."""
        pair = await get_pair(db_session, settings)
        adapter = StubAdapter([[]])
        adapter.raise_on_call = {0: IpBannedError("banned", retry_after_seconds=120.0)}
        service = CandleBackfillService(
            adapter, CandleRepository(db_session), clock=FixedClock(NOW)
        )

        with pytest.raises(IpBannedError):
            await service.backfill(
                trading_pair_id=pair.id,
                symbol="BTCUSDT",
                timeframe=M15,
                start=BASE,
                page_size=100,
            )

        assert len(adapter.calls) == 1

    async def test_an_empty_first_page_ends_the_backfill(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        adapter = StubAdapter([[]])
        service = CandleBackfillService(
            adapter, CandleRepository(db_session), clock=FixedClock(NOW)
        )

        result = await service.backfill(
            trading_pair_id=pair.id, symbol="BTCUSDT", timeframe=M15, start=BASE, page_size=100
        )

        assert result.candles_inserted == 0
        assert result.pages_fetched == 1

    async def test_the_start_is_aligned_before_the_first_request(
        self, db_session: AsyncSession, settings: Settings
    ) -> None:
        pair = await get_pair(db_session, settings)
        adapter = StubAdapter([[]])
        service = CandleBackfillService(
            adapter, CandleRepository(db_session), clock=FixedClock(NOW)
        )

        await service.backfill(
            trading_pair_id=pair.id,
            symbol="BTCUSDT",
            timeframe=M15,
            start=BASE + timedelta(minutes=7),
            page_size=100,
        )

        assert adapter.calls[0]["start"] == BASE
