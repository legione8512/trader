"""Binance Spot public market data adapter.

Public endpoints only. This adapter can never place an order: it has no
credentials and no method that could.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from app.core.logging import get_logger
from app.domain.enums import Timeframe
from app.domain.money import to_decimal
from app.domain.symbol_filters import parse_symbol_filters
from app.exchanges.base import Candle, ExchangeInfo, ExchangeSymbol
from app.exchanges.binance.constants import (
    EXCHANGE_INFO_PATH,
    KLINE_CLOSE,
    KLINE_CLOSE_TIME,
    KLINE_FIELD_COUNT,
    KLINE_HIGH,
    KLINE_LOW,
    KLINE_OPEN,
    KLINE_OPEN_TIME,
    KLINE_QUOTE_VOLUME,
    KLINE_TRADE_COUNT,
    KLINE_VOLUME,
    KLINES_MAX_LIMIT,
    KLINES_PATH,
    PING_PATH,
    SERVER_TIME_PATH,
    WEIGHT_EXCHANGE_INFO,
    WEIGHT_KLINES,
    WEIGHT_PING,
    WEIGHT_SERVER_TIME,
    BinanceInterval,
)
from app.exchanges.binance.rest import BinanceRestClient
from app.exchanges.errors import ExchangeDataError

logger = get_logger(__name__)

#: Our timeframes mapped onto the venue's interval strings. Explicit rather than
#: relying on the values happening to match: they do today, and a silent
#: mismatch tomorrow would subscribe us to the wrong candles.
TIMEFRAME_TO_INTERVAL: dict[Timeframe, BinanceInterval] = {
    Timeframe.M15: BinanceInterval.M15,
    Timeframe.H1: BinanceInterval.H1,
    Timeframe.H4: BinanceInterval.H4,
    Timeframe.D1: BinanceInterval.D1,
}


def milliseconds_to_utc(value: int) -> datetime:
    """The venue reports every timestamp in milliseconds since the epoch, UTC."""
    return datetime.fromtimestamp(value / 1000, tz=UTC)


def utc_to_milliseconds(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("Timestamps sent to the exchange must be timezone-aware")
    return int(value.timestamp() * 1000)


class BinanceMarketDataAdapter:
    """Reads public market data from Binance Spot."""

    def __init__(self, client: BinanceRestClient) -> None:
        self._client = client

    # ---------------------------------------------------------- connectivity ---

    async def ping(self) -> bool:
        await self._client.get(PING_PATH, weight=WEIGHT_PING)
        return True

    async def server_time(self) -> datetime:
        payload = await self._client.get(SERVER_TIME_PATH, weight=WEIGHT_SERVER_TIME)
        if not isinstance(payload, dict) or "serverTime" not in payload:
            raise ExchangeDataError("Server time response has no serverTime field")
        raw = payload["serverTime"]
        if not isinstance(raw, int):
            raise ExchangeDataError(f"serverTime is not an integer: {raw!r}")
        return milliseconds_to_utc(raw)

    # -------------------------------------------------------------- metadata ---

    async def exchange_info(self, symbols: list[str] | None = None) -> ExchangeInfo:
        """Symbols, filters and published rate limits.

        Weight 10, so this is fetched on startup and on a schedule, never per
        decision.
        """
        params: dict[str, Any] | None = None
        if symbols:
            # The venue expects a JSON array with no spaces for this parameter.
            quoted = ",".join(f'"{symbol.upper()}"' for symbol in symbols)
            params = {"symbols": f"[{quoted}]"}

        payload = await self._client.get(
            EXCHANGE_INFO_PATH, weight=WEIGHT_EXCHANGE_INFO, params=params
        )
        if not isinstance(payload, dict):
            raise ExchangeDataError("exchangeInfo did not return an object")

        server_time_raw = payload.get("serverTime")
        if not isinstance(server_time_raw, int):
            raise ExchangeDataError("exchangeInfo has no usable serverTime")

        raw_symbols = payload.get("symbols")
        if not isinstance(raw_symbols, list):
            raise ExchangeDataError("exchangeInfo has no symbols array")

        parsed: dict[str, ExchangeSymbol] = {}
        for entry in raw_symbols:
            if not isinstance(entry, dict):
                raise ExchangeDataError("A symbols[] entry is not an object")
            symbol = entry.get("symbol")
            if not isinstance(symbol, str):
                raise ExchangeDataError("A symbols[] entry has no symbol")
            raw_filters = entry.get("filters", [])
            if not isinstance(raw_filters, list):
                raise ExchangeDataError(f"{symbol} has a non-list filters field")

            parsed[symbol] = ExchangeSymbol(
                symbol=symbol,
                status=str(entry.get("status", "")),
                base_asset=str(entry.get("baseAsset", "")),
                quote_asset=str(entry.get("quoteAsset", "")),
                is_spot_trading_allowed=bool(entry.get("isSpotTradingAllowed", False)),
                filters=parse_symbol_filters(symbol, raw_filters),
                raw=entry,
            )

        rate_limits = payload.get("rateLimits", [])
        if not isinstance(rate_limits, list):
            rate_limits = []

        return ExchangeInfo(
            server_time=milliseconds_to_utc(server_time_raw),
            symbols=parsed,
            rate_limits=rate_limits,
        )

    # --------------------------------------------------------------- candles ---

    async def historical_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        """One page of closed candles, oldest first.

        Paging is the caller's concern: the documented maximum is 1000 per
        request, and a backfill spanning months is a scheduling decision, not
        something a single call should hide.
        """
        interval = TIMEFRAME_TO_INTERVAL.get(timeframe)
        if interval is None:
            raise ExchangeDataError(f"No exchange interval is mapped for {timeframe}")

        requested = KLINES_MAX_LIMIT if limit is None else limit
        if requested < 1 or requested > KLINES_MAX_LIMIT:
            raise ValueError(f"limit must be between 1 and {KLINES_MAX_LIMIT}, got {requested}")

        params: dict[str, Any] = {
            "symbol": symbol.upper(),
            "interval": interval.value,
            "limit": requested,
        }
        if start is not None:
            params["startTime"] = utc_to_milliseconds(start)
        if end is not None:
            params["endTime"] = utc_to_milliseconds(end)

        payload = await self._client.get(KLINES_PATH, weight=WEIGHT_KLINES, params=params)
        if not isinstance(payload, list):
            raise ExchangeDataError("klines did not return an array")

        return [parse_kline(symbol.upper(), timeframe, row) for row in payload]


def parse_kline(symbol: str, timeframe: Timeframe, row: Any) -> Candle:
    """Parse one kline row.

    The response is an ARRAY of 12 elements, not an object, so position is the
    contract. Prices and volumes arrive as STRINGS, which is what lets them
    become exact Decimals with no float ever involved.
    """
    if not isinstance(row, list):
        raise ExchangeDataError(f"A kline row is not an array: {row!r}")
    if len(row) < KLINE_FIELD_COUNT:
        raise ExchangeDataError(
            f"A kline row has {len(row)} fields, expected at least {KLINE_FIELD_COUNT}"
        )

    try:
        open_time = milliseconds_to_utc(int(row[KLINE_OPEN_TIME]))
        close_time = milliseconds_to_utc(int(row[KLINE_CLOSE_TIME]))
        candle = Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=open_time,
            close_time=close_time,
            open=to_decimal(row[KLINE_OPEN]),
            high=to_decimal(row[KLINE_HIGH]),
            low=to_decimal(row[KLINE_LOW]),
            close=to_decimal(row[KLINE_CLOSE]),
            volume=to_decimal(row[KLINE_VOLUME]),
            quote_volume=to_decimal(row[KLINE_QUOTE_VOLUME]),
            trade_count=int(row[KLINE_TRADE_COUNT]),
        )
    except ExchangeDataError:
        raise
    except Exception as exc:
        raise ExchangeDataError(f"Malformed kline row: {row!r}") from exc

    validate_candle(candle)
    return candle


def validate_candle(candle: Candle) -> None:
    """Refuse a candle whose numbers cannot all be true at once.

    A malformed candle is not a parsing inconvenience. Acting on it would mean
    trading on figures nobody checked, so it is rejected rather than repaired.
    """
    if candle.close_time <= candle.open_time:
        raise ExchangeDataError(
            f"{candle.symbol} candle closes at or before it opens: "
            f"{candle.open_time} -> {candle.close_time}"
        )
    for name, value in (
        ("open", candle.open),
        ("high", candle.high),
        ("low", candle.low),
        ("close", candle.close),
    ):
        if value <= 0:
            raise ExchangeDataError(f"{candle.symbol} candle has {name} <= 0: {value}")
    if candle.low > candle.high:
        raise ExchangeDataError(
            f"{candle.symbol} candle has low {candle.low} above high {candle.high}"
        )
    if not (candle.low <= candle.open <= candle.high):
        raise ExchangeDataError(f"{candle.symbol} candle open {candle.open} outside its range")
    if not (candle.low <= candle.close <= candle.high):
        raise ExchangeDataError(f"{candle.symbol} candle close {candle.close} outside its range")
    if candle.volume < 0 or candle.quote_volume < 0:
        raise ExchangeDataError(f"{candle.symbol} candle has negative volume")
    if candle.trade_count < 0:
        raise ExchangeDataError(f"{candle.symbol} candle has a negative trade count")


def clock_drift_milliseconds(local: datetime, exchange: datetime) -> Decimal:
    """Signed drift, positive when our clock is ahead of the exchange.

    Rule R-22. Drift matters even for public data: a candle judged fresh against
    a clock that is minutes wrong is a stale candle we believe in.
    """
    if local.tzinfo is None or exchange.tzinfo is None:
        raise ValueError("Clock drift needs timezone-aware timestamps")
    delta = local - exchange
    return to_decimal(str(delta.total_seconds() * 1000))
