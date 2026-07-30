"""The real WebSocket transport.

Isolated from ``stream.py`` on purpose: everything interesting about the stream
- when to rotate a connection, what to do with a malformed message, which
candles may be stored - lives there and is tested without a socket. This module
is the thin part that actually opens one.
"""

from __future__ import annotations

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from app.core.logging import get_logger
from app.exchanges.binance.constants import (
    STREAM_PONG_DEADLINE_SECONDS,
    STREAM_SERVER_PING_INTERVAL_SECONDS,
)
from app.exchanges.binance.stream import StreamClosedError
from app.exchanges.errors import ExchangeUnavailableError

logger = get_logger(__name__)

#: How long to wait for the opening handshake before giving up.
DEFAULT_OPEN_TIMEOUT_SECONDS = 10.0


class WebsocketConnection:
    """One open connection, narrowed to receive and close."""

    def __init__(self, connection: ClientConnection) -> None:
        self._connection = connection

    async def receive(self) -> str:
        try:
            message = await self._connection.recv()
        except ConnectionClosed as exc:
            raise StreamClosedError(f"Stream closed: {exc.__class__.__name__}") from exc
        except OSError as exc:
            raise StreamClosedError(f"Stream transport failed: {type(exc).__name__}") from exc
        if isinstance(message, bytes):
            return message.decode("utf-8")
        return message

    async def close(self) -> None:
        await self._connection.close()


class WebsocketStreamConnector:
    """Opens Binance market-data connections.

    The library answers the server's ping frames by itself, which is exactly
    what the documentation requires ("send a pong with a copy of ping's payload
    as soon as possible"). Our own keepalive ping is kept as an independent
    liveness check; at one frame per 20 seconds it stays far below the
    documented limit of 5 incoming messages per second.
    """

    def __init__(
        self,
        *,
        open_timeout_seconds: float = DEFAULT_OPEN_TIMEOUT_SECONDS,
        ping_interval_seconds: float = float(STREAM_SERVER_PING_INTERVAL_SECONDS),
        ping_timeout_seconds: float = float(STREAM_PONG_DEADLINE_SECONDS),
    ) -> None:
        self._open_timeout = open_timeout_seconds
        self._ping_interval = ping_interval_seconds
        self._ping_timeout = ping_timeout_seconds

    async def connect(self, url: str) -> WebsocketConnection:
        try:
            connection = await connect(
                url,
                open_timeout=self._open_timeout,
                ping_interval=self._ping_interval,
                ping_timeout=self._ping_timeout,
            )
        except (OSError, InvalidHandshake, TimeoutError) as exc:
            # Reported as unavailable rather than as a programming error: the
            # caller reconnects with backoff, it does not crash.
            raise ExchangeUnavailableError(
                f"Could not open the market data stream: {type(exc).__name__}"
            ) from exc
        return WebsocketConnection(connection)
