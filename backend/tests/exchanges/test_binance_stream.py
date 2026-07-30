"""Kline WebSocket stream: parsing, closure filtering and reconnection.

No socket is opened. The transport is a protocol, so the interesting parts -
what happens at the documented 24-hour limit, what happens when the connection
dies mid-stream, what happens to a candle that has not closed - are testable
deterministically.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.core.clock import FixedClock
from app.domain.enums import Timeframe
from app.exchanges.base import Candle
from app.exchanges.binance.constants import (
    STREAM_MAX_CONNECTION_SECONDS,
    STREAM_MAX_STREAMS_PER_CONNECTION,
    BinanceInterval,
    combined_stream_url,
    kline_stream_name,
)
from app.exchanges.binance.stream import (
    BinanceKlineStream,
    StreamClosedError,
    StreamConnection,
    StreamSubscription,
    parse_kline_event,
    unwrap_envelope,
)
from app.exchanges.errors import ExchangeDataError, ExchangeUnavailableError

M15 = Timeframe.M15
OPEN_TIME = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
OPEN_MS = int(OPEN_TIME.timestamp() * 1000)
CLOSE_MS = OPEN_MS + 15 * 60 * 1000 - 1


def kline_payload(
    *,
    is_closed: bool = True,
    open_ms: int = OPEN_MS,
    symbol: str = "BTCUSDT",
    interval: str = "15m",
    **overrides: Any,
) -> dict[str, Any]:
    """A kline event shaped exactly as the documentation describes it."""
    kline: dict[str, Any] = {
        "t": open_ms,
        "T": open_ms + 15 * 60 * 1000 - 1,
        "s": symbol,
        "i": interval,
        "f": 100,
        "L": 200,
        "o": "65000.00000000",
        "c": "65100.50000000",
        "h": "65250.00000000",
        "l": "64900.00000000",
        "v": "12.34567000",
        "n": 1543,
        "x": is_closed,
        "q": "803210.12345678",
        "V": "6.00000000",
        "Q": "400000.00000000",
        "B": "0",
    }
    kline.update(overrides)
    return {"e": "kline", "E": open_ms + 1000, "s": symbol, "k": kline}


def envelope(payload: dict[str, Any], stream: str = "btcusdt@kline_15m") -> str:
    return json.dumps({"stream": stream, "data": payload})


class FakeConnection:
    """Serves prepared messages, then raises whatever was configured."""

    def __init__(self, messages: Sequence[str], ends_with: Exception | None = None) -> None:
        self._messages = list(messages)
        self._ends_with = ends_with
        self.closed = False

    async def receive(self) -> str:
        if self._messages:
            return self._messages.pop(0)
        raise self._ends_with or StreamClosedError("no more messages")

    async def close(self) -> None:
        self.closed = True


class SilentConnection:
    """Never answers. Stands in for a socket that is open but dead."""

    def __init__(self) -> None:
        self.closed = False

    async def receive(self) -> str:
        await asyncio.sleep(10)
        return ""

    async def close(self) -> None:
        self.closed = True


class FakeConnector:
    """Hands out prepared connections in order."""

    def __init__(self, connections: Sequence[StreamConnection | Exception]) -> None:
        self._connections = list(connections)
        self.urls: list[str] = []

    async def connect(self, url: str) -> StreamConnection:
        self.urls.append(url)
        if not self._connections:
            raise ExchangeUnavailableError("no more connections prepared")
        nxt = self._connections.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class SteppingClock:
    """Advances a fixed amount every time it is read."""

    def __init__(self, start: datetime, step_seconds: float) -> None:
        self._now = start
        self._step = timedelta(seconds=step_seconds)

    def now(self) -> datetime:
        current = self._now
        self._now += self._step
        return current


def build_stream(
    connector: FakeConnector,
    *,
    clock: Any = None,
    subscriptions: Sequence[StreamSubscription] | None = None,
    lifetime_seconds: float = 3600.0,
) -> BinanceKlineStream:
    return BinanceKlineStream(
        connector,
        subscriptions or [StreamSubscription("BTCUSDT", M15)],
        clock=clock or FixedClock(OPEN_TIME),
        connection_lifetime_seconds=lifetime_seconds,
        # No waiting in tests. The backoff itself is asserted separately.
        backoff_seconds=(0.0,),
    )


async def take(stream: BinanceKlineStream, count: int) -> list[Candle]:
    collected: list[Candle] = []
    async for candle in stream.closed_candles():
        collected.append(candle)
        if len(collected) == count:
            break
    return collected


class TestStreamNames:
    def test_a_stream_name_is_lowercase(self) -> None:
        """REST symbols are uppercase, stream names are lowercase. Mixing them
        up subscribes to nothing, silently."""
        assert kline_stream_name("BTCUSDT", BinanceInterval.M15) == "btcusdt@kline_15m"

    def test_a_combined_url_joins_names_with_a_slash(self) -> None:
        url = combined_stream_url(["btcusdt@kline_15m", "ethusdt@kline_15m"])
        assert url.endswith("/stream?streams=btcusdt@kline_15m/ethusdt@kline_15m")

    def test_an_empty_subscription_list_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one"):
            combined_stream_url([])

    def test_the_documented_stream_limit_is_enforced_before_connecting(self) -> None:
        too_many = [f"s{index}@kline_15m" for index in range(STREAM_MAX_STREAMS_PER_CONNECTION + 1)]
        with pytest.raises(ValueError, match="at most 1024"):
            combined_stream_url(too_many)

    def test_a_subscription_derives_its_own_stream_name(self) -> None:
        assert StreamSubscription("ethusdt", M15).stream_name == "ethusdt@kline_15m"


class TestEnvelope:
    def test_a_combined_message_is_unwrapped(self) -> None:
        payload = unwrap_envelope(envelope(kline_payload()))
        assert payload["e"] == "kline"

    def test_a_raw_message_passes_through(self) -> None:
        """A single-stream connection sends the payload with no wrapper."""
        payload = unwrap_envelope(json.dumps(kline_payload()))
        assert payload["e"] == "kline"

    def test_invalid_json_is_refused(self) -> None:
        with pytest.raises(ExchangeDataError, match="not valid JSON"):
            unwrap_envelope("{not json")

    def test_a_json_array_is_refused(self) -> None:
        with pytest.raises(ExchangeDataError, match="not an object"):
            unwrap_envelope("[1, 2, 3]")


class TestKlineEventParsing:
    def test_prices_arrive_as_exact_decimals(self) -> None:
        event = parse_kline_event(kline_payload())
        assert event is not None
        assert event.candle.close == Decimal("65100.50000000")
        assert event.candle.quote_volume == Decimal("803210.12345678")
        assert event.candle.trade_count == 1543

    def test_the_closed_flag_is_carried_through(self) -> None:
        closed = parse_kline_event(kline_payload(is_closed=True))
        forming = parse_kline_event(kline_payload(is_closed=False))
        assert closed is not None and forming is not None
        assert closed.is_closed is True
        assert forming.is_closed is False

    def test_a_non_boolean_closed_flag_is_refused(self) -> None:
        """Never coerced: bool("false") is True and bool(None) is False, so a
        coercion would decide the look-ahead question by accident."""
        with pytest.raises(ExchangeDataError, match="non-boolean x"):
            parse_kline_event(kline_payload(x="true"))

    def test_a_missing_closed_flag_is_refused(self) -> None:
        payload = kline_payload()
        del payload["k"]["x"]
        with pytest.raises(ExchangeDataError, match="non-boolean x"):
            parse_kline_event(payload)

    def test_an_event_of_another_type_is_skipped_not_rejected(self) -> None:
        """Control replies share the socket. They must not stop the feed."""
        assert parse_kline_event({"result": None, "id": 1}) is None
        assert parse_kline_event({"e": "aggTrade", "s": "BTCUSDT"}) is None

    def test_an_interval_we_do_not_model_is_skipped(self) -> None:
        """Skipped rather than stored: filing a 1m candle as 15m would be worse
        than ignoring it."""
        assert parse_kline_event(kline_payload(interval="1m")) is None

    def test_the_timeframe_comes_from_the_event_not_from_the_subscription(self) -> None:
        event = parse_kline_event(kline_payload(interval="1h"))
        assert event is not None
        assert event.timeframe is Timeframe.H1

    def test_a_kline_event_without_its_k_object_is_refused(self) -> None:
        with pytest.raises(ExchangeDataError, match="no k object"):
            parse_kline_event({"e": "kline", "E": OPEN_MS, "s": "BTCUSDT"})

    def test_a_closed_candle_with_impossible_numbers_is_refused(self) -> None:
        """The same validation the REST path applies. A high below the low
        cannot be true whichever transport delivered it."""
        with pytest.raises(ExchangeDataError, match="above high"):
            parse_kline_event(kline_payload(h="1.0", l="2.0"))

    def test_a_forming_candle_is_not_validated_as_a_closed_one(self) -> None:
        """Its close time is legitimately in the future and its volumes are
        still growing; judging it by closed-candle rules would reject good data."""
        event = parse_kline_event(kline_payload(is_closed=False, v="0", n=0))
        assert event is not None
        assert event.candle.volume == Decimal("0")

    def test_a_malformed_price_is_refused(self) -> None:
        with pytest.raises(ExchangeDataError, match="Malformed kline event"):
            parse_kline_event(kline_payload(c="not-a-number"))


class TestClosedCandleFiltering:
    async def test_only_closed_candles_are_yielded(self) -> None:
        """The heart of this milestone. Every update before x is true repaints."""
        messages = [
            envelope(kline_payload(is_closed=False)),
            envelope(kline_payload(is_closed=False, c="65111.00000000")),
            envelope(kline_payload(is_closed=True, c="65123.00000000")),
        ]
        connector = FakeConnector([FakeConnection(messages)])

        candles = await take(build_stream(connector), 1)

        assert len(candles) == 1
        assert candles[0].close == Decimal("65123.00000000")

    async def test_forming_candles_are_available_through_events(self) -> None:
        """Useful for a live price display, never for storage or a decision."""
        connector = FakeConnector([FakeConnection([envelope(kline_payload(is_closed=False))])])
        stream = build_stream(connector)

        seen = []
        async for event in stream.events():
            seen.append(event)
            break

        assert seen[0].is_closed is False

    async def test_a_malformed_message_does_not_kill_the_feed(self) -> None:
        """A parser that stops on one bad message turns a hiccup into an outage."""
        messages = ["{not json}", envelope(kline_payload())]
        connector = FakeConnector([FakeConnection(messages)])

        candles = await take(build_stream(connector), 1)

        assert len(candles) == 1


class TestReconnection:
    async def test_a_dropped_connection_is_reconnected(self) -> None:
        first = FakeConnection([envelope(kline_payload(open_ms=OPEN_MS))])
        second = FakeConnection([envelope(kline_payload(open_ms=OPEN_MS + 15 * 60 * 1000))])
        connector = FakeConnector([first, second])

        candles = await take(build_stream(connector), 2)

        assert len(candles) == 2
        assert candles[1].open_time == OPEN_TIME + timedelta(minutes=15)
        assert first.closed is True

    async def test_a_failed_connect_is_retried(self) -> None:
        connector = FakeConnector(
            [
                ExchangeUnavailableError("dns failure"),
                FakeConnection([envelope(kline_payload())]),
            ]
        )

        candles = await take(build_stream(connector), 1)

        assert len(candles) == 1
        assert len(connector.urls) == 2

    async def test_the_connection_is_closed_when_the_consumer_stops(self) -> None:
        """Otherwise a socket leaks on every shutdown."""
        connection = FakeConnection(
            [envelope(kline_payload()), envelope(kline_payload(open_ms=OPEN_MS + 900_000))]
        )
        connector = FakeConnector([connection])
        stream = build_stream(connector)

        generator = stream.closed_candles()
        await anext(generator)
        await generator.aclose()

        assert connection.closed is True

    async def test_the_connection_is_rotated_while_it_is_still_healthy(self) -> None:
        """A connection is only valid for 24 hours by documentation. It is
        replaced before that, not when it dies mid-candle. The first socket here
        still has messages queued when it is rotated away."""
        first = FakeConnection([envelope(kline_payload()) for _ in range(5)])
        second = FakeConnection([envelope(kline_payload(open_ms=OPEN_MS + 900_000))])
        connector = FakeConnector([first, second])

        # Every clock read jumps ten minutes, so a thirty-minute lifetime runs
        # out after two messages even though the socket is perfectly healthy.
        stream = build_stream(
            connector, clock=SteppingClock(OPEN_TIME, 600), lifetime_seconds=1800.0
        )

        candles = await take(stream, 3)

        assert len(candles) == 3
        assert first.closed is True
        assert len(connector.urls) == 2

    def test_the_default_lifetime_stops_short_of_the_documented_limit(self) -> None:
        """Rotating exactly at the limit is rotating after being disconnected."""
        stream = BinanceKlineStream(
            FakeConnector([]), [StreamSubscription("BTCUSDT", M15)], clock=FixedClock(OPEN_TIME)
        )
        lifetime = stream.connection_lifetime.total_seconds()
        assert lifetime < STREAM_MAX_CONNECTION_SECONDS
        # The margin must exceed one candle, or the replacement connection would
        # race the close of the candle we are waiting for.
        margin = STREAM_MAX_CONNECTION_SECONDS - lifetime
        assert margin > M15.duration.total_seconds()

    async def test_silence_longer_than_the_timeout_is_treated_as_a_dead_socket(self) -> None:
        """A quiet market still produces an update every two seconds and a
        server ping every twenty. Complete silence is a broken connection."""
        silent = SilentConnection()
        replacement = FakeConnection([envelope(kline_payload())])
        connector = FakeConnector([silent, replacement])
        stream = BinanceKlineStream(
            connector,
            [StreamSubscription("BTCUSDT", M15)],
            clock=FixedClock(OPEN_TIME),
            receive_timeout_seconds=0.01,
            backoff_seconds=(0.0,),
        )

        candles = await take(stream, 1)

        assert len(candles) == 1
        assert silent.closed is True


class TestConstruction:
    def test_a_stream_without_subscriptions_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one subscription"):
            BinanceKlineStream(FakeConnector([]), [])

    def test_several_symbols_share_one_connection(self) -> None:
        stream = BinanceKlineStream(
            FakeConnector([]),
            [StreamSubscription("BTCUSDT", M15), StreamSubscription("ETHUSDT", M15)],
        )
        assert "btcusdt@kline_15m/ethusdt@kline_15m" in stream.url

    def test_a_subscription_resolves_its_exchange_interval(self) -> None:
        assert StreamSubscription("BTCUSDT", M15).interval is BinanceInterval.M15
        assert StreamSubscription("BTCUSDT", Timeframe.H4).interval is BinanceInterval.H4
