"""Candle storage.

Separate from ``repositories.py`` because the access pattern is different: this
is a time series written in bulk and read in ranges, not an aggregate loaded by
identity.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.enums import CandleSource, Timeframe
from app.exchanges.base import Candle as CandleValue
from app.persistence.mixins import utc_now
from app.persistence.models.market_data import Candle


class CandleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_ignoring_duplicates(
        self,
        trading_pair_id: uuid.UUID,
        candles: Sequence[CandleValue],
        source: CandleSource,
    ) -> int:
        """Store candles, skipping any already present. Returns rows inserted.

        ``ON CONFLICT DO NOTHING`` rather than an update, because a closed
        candle is immutable: its numbers were settled the moment it closed.
        Re-fetching a range after a restart therefore costs nothing and changes
        nothing, which is what makes a backfill safe to re-run.
        """
        if not candles:
            return 0

        ingested_at = utc_now()
        rows = [
            {
                "trading_pair_id": trading_pair_id,
                "timeframe": candle.timeframe,
                "open_time": candle.open_time,
                "close_time": candle.close_time,
                "open_price": candle.open,
                "high_price": candle.high,
                "low_price": candle.low,
                "close_price": candle.close,
                "volume": candle.volume,
                "quote_volume": candle.quote_volume,
                "trade_count": candle.trade_count,
                "source": source,
                "ingested_at": ingested_at,
            }
            for candle in candles
        ]

        statement = (
            postgres_insert(Candle)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["trading_pair_id", "timeframe", "open_time"])
            # RETURNING yields a row only for what was actually written, so the
            # count is the number of genuinely new candles rather than the number
            # offered. Skipped duplicates are simply absent from the result.
            .returning(Candle.open_time)
        )
        result = await self._session.execute(statement)
        inserted = len(result.scalars().all())
        await self._session.flush()
        return inserted

    async def latest(self, trading_pair_id: uuid.UUID, timeframe: Timeframe) -> Candle | None:
        """The newest stored candle. Served by the primary key index, backwards."""
        result = await self._session.execute(
            select(Candle)
            .where(Candle.trading_pair_id == trading_pair_id, Candle.timeframe == timeframe)
            .order_by(Candle.open_time.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def earliest(self, trading_pair_id: uuid.UUID, timeframe: Timeframe) -> Candle | None:
        result = await self._session.execute(
            select(Candle)
            .where(Candle.trading_pair_id == trading_pair_id, Candle.timeframe == timeframe)
            .order_by(Candle.open_time)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def range(
        self,
        trading_pair_id: uuid.UUID,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> Sequence[Candle]:
        """Candles with ``start <= open_time <= end``, oldest first."""
        result = await self._session.execute(
            select(Candle)
            .where(
                Candle.trading_pair_id == trading_pair_id,
                Candle.timeframe == timeframe,
                Candle.open_time >= start,
                Candle.open_time <= end,
            )
            .order_by(Candle.open_time)
        )
        return result.scalars().all()

    async def open_times(
        self,
        trading_pair_id: uuid.UUID,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list[datetime]:
        """Just the open times, for gap detection without loading whole rows."""
        result = await self._session.execute(
            select(Candle.open_time)
            .where(
                Candle.trading_pair_id == trading_pair_id,
                Candle.timeframe == timeframe,
                Candle.open_time >= start,
                Candle.open_time <= end,
            )
            .order_by(Candle.open_time)
        )
        return list(result.scalars().all())

    async def count(self, trading_pair_id: uuid.UUID, timeframe: Timeframe) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(Candle)
            .where(Candle.trading_pair_id == trading_pair_id, Candle.timeframe == timeframe)
        )
        return int(result.scalar_one())
