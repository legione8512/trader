"""Binance Spot kline WebSocket stream.

Three documented facts shape everything here:

1. **"A single connection to stream.binance.com is only valid for 24 hours;
   expect to be disconnected at the 24 hour mark."** So the rotation is
   scheduled *before* the limit rather than discovered when the socket dies.
2. **The server sends a ping every 20 seconds and disconnects if no pong comes
   back within a minute.** The transport answers pings automatically; nothing
   in this module sends unsolicited traffic, because the connection also has a
   documented limit of 5 incoming messages per second.
3. **A kline event carries ``k.x``, "is this kline closed?"**. Every update
   before that flag turns true is provisional and repaints. Only closed candles
   leave this module.

The transport is injected as a protocol. The reconnection logic, the deadline
arithmetic and the parsing are then testable without a socket, which is the
only way to test "what happens after 24 hours" at all.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from app.core.clock import Clock, SystemClock
from app.core.logging import get_logger
from app.domain.enums import Timeframe
from app.domain.errors import MoneyError
from app.domain.money import to_decimal
from app.exchanges.base import Candle
from app.exchanges.binance.constants import (
    EVENT_KLINE_KEY,
    EVENT_TIME_KEY,
    EVENT_TYPE_KEY,
    KLINE_EVENT_CLOSE,
    KLINE_EVENT_CLOSE_TIME,
    KLINE_EVENT_HIGH,
    KLINE_EVENT_INTERVAL,
    KLINE_EVENT_IS_CLOSED,
    KLINE_EVENT_LOW,
    KLINE_EVENT_OPEN,
    KLINE_EVENT_OPEN_TIME,
    KLINE_EVENT_QUOTE_VOLUME,
    KLINE_EVENT_SYMBOL,
    KLINE_EVENT_TRADE_COUNT,
    KLINE_EVENT_TYPE,
    KLINE_EVENT_VOLUME,
    STREAM_ENVELOPE_DATA_KEY,
    STREAM_MAX_CONNECTION_SECONDS,
    STREAM_PONG_DEADLINE_SECONDS,
    STREAM_RECONNECT_MARGIN_SECONDS,
    BinanceInterval,
    combined_stream_url,
    kline_stream_name,
)
from app.exchanges.binance.market_data import (
    TIMEFRAME_TO_INTERVAL,
    milliseconds_to_utc,
    validate_candle,
)
from app.exchanges.errors import ExchangeDataError, ExchangeUnavailableError

logger = get_logger(__name__)

#: How long to wait for a message before declaring the connection dead. The
#: documented update speed is 2 seconds and the server pings every 20, so a
#: minute of complete silence is not a quiet market - it is a broken socket.
DEFAULT_RECEIVE_TIMEOUT_SECONDS = float(STREAM_PONG_DEADLINE_SECONDS)

#: Backoff between reconnection attempts, in seconds. Capped: a stream that has
#: been down for an hour must still retry every minute, because the operator
#: needs the feed back, and the exchange is not helped by us waiting longer.
RECONNECT_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 5.0, 15.0, 30.0, 60.0)


class StreamClosedError(ExchangeUnavailableError):
    """The connection ended. Expected at least once a day, never fatal."""


@runtime_checkable
class StreamConnection(Protocol):
    """One live WebSocket connection, reduced to what this module needs."""

    async def receive(self) -> str:
        """The next message. Raises ``StreamClosedError`` when the socket ends."""
        ...

    async def close(self) -> None: ...


@runtime_checkable
class StreamConnector(Protocol):
    """Opens connections. Injected so the reconnection logic can be tested."""

    async def connect(self, url: str) -> StreamConnection: ...


@dataclass(frozen=True, slots=True)
class KlineEvent:
    """One kline update, closed or still forming."""

    symbol: str
    timeframe: Timeframe
    event_time: datetime
    is_closed: bool
    candle: Candle


@dataclass(frozen=True, slots=True)
class StreamSubscription:
    """One symbol followed on one timeframe."""

    symbol: str
    timeframe: Timeframe

    @property
    def interval(self) -> BinanceInterval:
        interval = TIMEFRAME_TO_INTERVAL.get(self.timeframe)
        if interval is None:
            raise ExchangeDataError(f"No exchange interval is mapped for {self.timeframe}")
        return interval

    @property
    def stream_name(self) -> str:
        return kline_stream_name(self.symbol, self.interval)


#: Reverse of TIMEFRAME_TO_INTERVAL. The event echoes the interval back as a
#: string, and it is checked rather than assumed: a mismatch would mean candles
#: of one length filed under another.
INTERVAL_TO_TIMEFRAME: dict[str, Timeframe] = {
    interval.value: timeframe for timeframe, interval in TIMEFRAME_TO_INTERVAL.items()
}


def unwrap_envelope(message: str) -> dict[str, Any]:
    """Return the payload, whether the message is combined or raw.

    A combined stream wraps every payload as
    ``{"stream":"<streamName>","data":<rawPayload>}``; a single-stream
    connection sends the payload directly. Accepting both means the caller does
    not have to know which kind of URL it was given.
    """
    try:
        parsed = json.loads(message)
    except json.JSONDecodeError as exc:
        raise ExchangeDataError("Stream message is not valid JSON") from exc

    if not isinstance(parsed, dict):
        raise ExchangeDataError(f"Stream message is not an object: {type(parsed).__name__}")

    data = parsed.get(STREAM_ENVELOPE_DATA_KEY)
    if isinstance(data, dict):
        return data
    return parsed


def parse_kline_event(payload: dict[str, Any]) -> KlineEvent | None:
    """Parse a kline event, or return ``None`` if this is not one.

    Control replies to SUBSCRIBE and events of other types share the socket.
    They are not errors and must not stop the stream, so they are skipped rather
    than rejected.
    """
    if payload.get(EVENT_TYPE_KEY) != KLINE_EVENT_TYPE:
        return None

    kline = payload.get(EVENT_KLINE_KEY)
    if not isinstance(kline, dict):
        raise ExchangeDataError("Kline event has no k object")

    raw_interval = kline.get(KLINE_EVENT_INTERVAL)
    timeframe = INTERVAL_TO_TIMEFRAME.get(str(raw_interval))
    if timeframe is None:
        # Not a failure: a connection may legitimately carry intervals we do not
        # model. Storing one under the wrong timeframe would be the failure.
        return None

    symbol = kline.get(KLINE_EVENT_SYMBOL)
    if not isinstance(symbol, str):
        raise ExchangeDataError("Kline event has no symbol")

    is_closed = kline.get(KLINE_EVENT_IS_CLOSED)
    if not isinstance(is_closed, bool):
        # Never coerced. A missing or non-boolean x would become False under
        # bool() for None and True for the string "false", and either way the
        # decision about look-ahead bias would be taken by an accident.
        raise ExchangeDataError(f"Kline event has a non-boolean x field: {is_closed!r}")

    try:
        candle = Candle(
            symbol=symbol,
            timeframe=timeframe,
            open_time=milliseconds_to_utc(int(kline[KLINE_EVENT_OPEN_TIME])),
            close_time=milliseconds_to_utc(int(kline[KLINE_EVENT_CLOSE_TIME])),
            open=to_decimal(kline[KLINE_EVENT_OPEN]),
            high=to_decimal(kline[KLINE_EVENT_HIGH]),
            low=to_decimal(kline[KLINE_EVENT_LOW]),
            close=to_decimal(kline[KLINE_EVENT_CLOSE]),
            volume=to_decimal(kline[KLINE_EVENT_VOLUME]),
            quote_volume=to_decimal(kline[KLINE_EVENT_QUOTE_VOLUME]),
            trade_count=int(kline[KLINE_EVENT_TRADE_COUNT]),
        )
        event_time = milliseconds_to_utc(int(payload[EVENT_TIME_KEY]))
    except ExchangeDataError:
        raise
    except (KeyError, TypeError, ValueError, ArithmeticError, MoneyError) as exc:
        # MoneyError included deliberately: an unparseable price string is a
        # malformed event, not a domain rule violation, and the caller upstream
        # only knows how to skip ExchangeDataError.
        raise ExchangeDataError(f"Malformed kline event: {exc}") from exc

    # Only a closed candle is validated as a candle. A forming one legitimately
    # has a close_time in the future and volumes that are still growing.
    if is_closed:
        validate_candle(candle)

    return KlineEvent(
        symbol=symbol,
        timeframe=timeframe,
        event_time=event_time,
        is_closed=is_closed,
        candle=candle,
    )


class BinanceKlineStream:
    """Yields closed candles from a live kline feed, reconnecting as needed."""

    def __init__(
        self,
        connector: StreamConnector,
        subscriptions: Sequence[StreamSubscription],
        *,
        clock: Clock | None = None,
        base_url: str | None = None,
        receive_timeout_seconds: float = DEFAULT_RECEIVE_TIMEOUT_SECONDS,
        connection_lifetime_seconds: float = (
            STREAM_MAX_CONNECTION_SECONDS - STREAM_RECONNECT_MARGIN_SECONDS
        ),
        backoff_seconds: Sequence[float] = RECONNECT_BACKOFF_SECONDS,
    ) -> None:
        if not subscriptions:
            raise ValueError("A stream needs at least one subscription")
        self._connector = connector
        self._subscriptions = tuple(subscriptions)
        self._clock = clock if clock is not None else SystemClock()
        self._receive_timeout = receive_timeout_seconds
        self._lifetime = timedelta(seconds=connection_lifetime_seconds)
        self._backoff = tuple(backoff_seconds) or (1.0,)
        names = [subscription.stream_name for subscription in self._subscriptions]
        self._url = (
            combined_stream_url(names) if base_url is None else combined_stream_url(names, base_url)
        )

    @property
    def url(self) -> str:
        return self._url

    @property
    def connection_lifetime(self) -> timedelta:
        """How long one connection is kept before being rotated."""
        return self._lifetime

    async def closed_candles(self) -> AsyncGenerator[Candle, None]:
        """Every candle that closes, in arrival order, across reconnections.

        Runs until the consumer stops iterating. A dropped connection is not an
        error the caller has to handle: it is reconnected, and the gap it left
        is the caller's problem to repair from REST.
        """
        events = self.events()
        try:
            async for event in events:
                if event.is_closed:
                    yield event.candle
        finally:
            # Closing this generator does NOT close the one it iterates. Without
            # this the inner generator stays suspended and its socket stays open
            # until the event loop finalises it, which is well after shutdown
            # believed itself finished.
            await events.aclose()

    async def events(self) -> AsyncGenerator[KlineEvent, None]:
        """Every kline update, closed or forming.

        Forming candles are useful for a live price display; they must never
        reach storage or a strategy decision.
        """
        failures = 0
        while True:
            try:
                connection = await self._connector.connect(self._url)
            except ExchangeUnavailableError as exc:
                failures += 1
                await self._wait_before_retry(failures, "connect_failed", exc)
                continue

            logger.info(
                "stream_connected",
                streams=[s.stream_name for s in self._subscriptions],
                rotate_after_seconds=self._lifetime.total_seconds(),
            )
            failures = 0
            # The rotation deadline is ours, set short of the documented 24
            # hours, so the socket is replaced while it still works rather than
            # dying mid-candle.
            deadline = self._clock.now() + self._lifetime
            try:
                while self._clock.now() < deadline:
                    try:
                        message = await asyncio.wait_for(
                            connection.receive(), self._receive_timeout
                        )
                    except TimeoutError as exc:
                        # A quiet market still produces an update every two
                        # seconds and a server ping every twenty. Complete
                        # silence is a dead socket, not a calm one.
                        raise StreamClosedError(
                            f"No stream message for {self._receive_timeout} seconds"
                        ) from exc

                    try:
                        event = parse_kline_event(unwrap_envelope(message))
                    except ExchangeDataError as exc:
                        # One bad message must not kill the feed, but it must be
                        # visible: a parser that silently drops events looks
                        # exactly like an exchange that stopped sending them.
                        logger.warning("stream_message_rejected", reason=str(exc))
                        continue

                    if event is not None:
                        yield event

                logger.info("stream_rotating", reason="approaching_documented_connection_limit")
            except ExchangeUnavailableError as exc:
                failures += 1
                logger.warning("stream_disconnected", reason=type(exc).__name__)
                await self._wait_before_retry(failures, "stream_disconnected", exc)
            finally:
                await self._safely_close(connection)

    async def _wait_before_retry(self, failures: int, reason: str, error: Exception) -> None:
        delay = self._backoff[min(failures, len(self._backoff)) - 1]
        logger.warning(
            "stream_reconnecting",
            reason=reason,
            error=type(error).__name__,
            attempt=failures,
            delay_seconds=delay,
        )
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    async def _safely_close(connection: StreamConnection) -> None:
        try:
            await connection.close()
        except Exception as exc:  # a failed close must not mask the real reason
            logger.debug("stream_close_failed", error=type(exc).__name__)
