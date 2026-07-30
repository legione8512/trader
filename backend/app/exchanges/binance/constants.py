"""Binance Spot constants, taken from the official documentation.

Every value here was read from the current official documentation immediately
before it was written down, never from memory. The source is quoted next to each
group so a future reader can re-verify it rather than trust this file.

Sources:
  https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints
  https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints
  https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information
  https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams
  https://developers.binance.com/docs/binance-spot-api-docs/filters
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

#: Primary REST base. The documentation also lists api-gcp and api1-api4, noting
#: that api1-api4 "should give better performance but have less stability".
#: Stability is worth more than latency to us, so the primary is the default.
REST_BASE_URL: Final = "https://api.binance.com"

#: Market data streams. Port 443 is used rather than 9443 because it survives
#: restrictive outbound firewalls; both are listed as valid.
STREAM_BASE_URL: Final = "wss://stream.binance.com:443"

PING_PATH: Final = "/api/v3/ping"
SERVER_TIME_PATH: Final = "/api/v3/time"
EXCHANGE_INFO_PATH: Final = "/api/v3/exchangeInfo"
KLINES_PATH: Final = "/api/v3/klines"
DEPTH_PATH: Final = "/api/v3/depth"


# ---------------------------------------------------------------------------
# Request weights
# ---------------------------------------------------------------------------
#
# Weight is per IP and is what a 429 counts. Budgeting before sending is the
# only way to avoid a 418, which is an IP ban escalating from 2 minutes to 3
# days for repeat offenders.

WEIGHT_PING: Final = 1
WEIGHT_SERVER_TIME: Final = 1
WEIGHT_EXCHANGE_INFO: Final = 10
WEIGHT_KLINES: Final = 2


def depth_weight(limit: int) -> int:
    """Weight of a depth request, which depends on the requested limit."""
    if limit <= 100:
        return 5
    if limit <= 500:
        return 25
    if limit <= 1000:
        return 50
    return 250


# ---------------------------------------------------------------------------
# Klines
# ---------------------------------------------------------------------------

#: Documented maximum and default for GET /api/v3/klines.
KLINES_MAX_LIMIT: Final = 1000
KLINES_DEFAULT_LIMIT: Final = 500

#: The response is a 12-element ARRAY, not an object, so the order is the
#: contract. Index 11 is documented as "Ignore".
KLINE_FIELD_COUNT: Final = 12

KLINE_OPEN_TIME: Final = 0
KLINE_OPEN: Final = 1
KLINE_HIGH: Final = 2
KLINE_LOW: Final = 3
KLINE_CLOSE: Final = 4
KLINE_VOLUME: Final = 5
KLINE_CLOSE_TIME: Final = 6
KLINE_QUOTE_VOLUME: Final = 7
KLINE_TRADE_COUNT: Final = 8
KLINE_TAKER_BUY_BASE_VOLUME: Final = 9
KLINE_TAKER_BUY_QUOTE_VOLUME: Final = 10


class BinanceInterval(StrEnum):
    """Intervals accepted by the klines endpoint and the kline stream."""

    S1 = "1s"
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M15 = "15m"
    M30 = "30m"
    H1 = "1h"
    H2 = "2h"
    H4 = "4h"
    H6 = "6h"
    H8 = "8h"
    H12 = "12h"
    D1 = "1d"
    D3 = "3d"
    W1 = "1w"
    MO1 = "1M"


# ---------------------------------------------------------------------------
# Symbols and filters
# ---------------------------------------------------------------------------


class SymbolStatus(StrEnum):
    """Documented symbol statuses. Only TRADING accepts orders."""

    TRADING = "TRADING"
    HALT = "HALT"
    BREAK = "BREAK"


class FilterType(StrEnum):
    """Symbol filter types.

    Note that BOTH ``MIN_NOTIONAL`` and ``NOTIONAL`` exist. They are different
    filters with different fields, and a symbol may carry either. Handling only
    one of them is a silent way to size an order the exchange will reject.
    """

    PRICE_FILTER = "PRICE_FILTER"
    PERCENT_PRICE_BY_SIDE = "PERCENT_PRICE_BY_SIDE"
    LOT_SIZE = "LOT_SIZE"
    MIN_NOTIONAL = "MIN_NOTIONAL"
    NOTIONAL = "NOTIONAL"
    MARKET_LOT_SIZE = "MARKET_LOT_SIZE"
    MAX_NUM_ORDERS = "MAX_NUM_ORDERS"
    MAX_NUM_ALGO_ORDERS = "MAX_NUM_ALGO_ORDERS"
    ICEBERG_PARTS = "ICEBERG_PARTS"
    TRAILING_DELTA = "TRAILING_DELTA"


# ---------------------------------------------------------------------------
# Rate limiting and errors
# ---------------------------------------------------------------------------

#: 429: rate limit exceeded, with a Retry-After header.
HTTP_TOO_MANY_REQUESTS: Final = 429
#: 418: the IP has been banned after repeated violations. Duration escalates
#: from 2 minutes to 3 days. Never retry through this.
HTTP_IP_BANNED: Final = 418

#: Prefix of the header reporting weight used, for example
#: X-MBX-USED-WEIGHT-1M. The suffix is (intervalNum)(intervalLetter).
USED_WEIGHT_HEADER_PREFIX: Final = "x-mbx-used-weight-"
RETRY_AFTER_HEADER: Final = "retry-after"


# ---------------------------------------------------------------------------
# WebSocket
# ---------------------------------------------------------------------------

#: "A single connection to stream.binance.com is only valid for 24 hours;
#: expect to be disconnected at the 24 hour mark." Reconnection is therefore
#: scheduled, not merely handled on error.
STREAM_MAX_CONNECTION_SECONDS: Final = 24 * 60 * 60

#: Reconnect this long before the documented limit, so the rotation happens on
#: our terms rather than mid-candle.
STREAM_RECONNECT_MARGIN_SECONDS: Final = 15 * 60

#: "The WebSocket server will send a ping frame every 20 seconds. If the
#: WebSocket server does not receive a pong frame back from the connection
#: within a minute the connection will be disconnected."
STREAM_SERVER_PING_INTERVAL_SECONDS: Final = 20
STREAM_PONG_DEADLINE_SECONDS: Final = 60


def kline_stream_name(symbol: str, interval: BinanceInterval) -> str:
    """Stream name for a kline feed, for example ``btcusdt@kline_15m``.

    The symbol is lowercased: stream names are lowercase while REST symbols are
    uppercase, and mixing them up yields a silent subscription to nothing.
    """
    return f"{symbol.lower()}@kline_{interval.value}"
