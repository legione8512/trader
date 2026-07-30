"""Binance market data adapter tests.

No network. Every response is a fixture served through ``httpx2.MockTransport``,
so the suite is deterministic and can run offline. The fixtures follow the shapes
quoted in the official documentation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx2
import pytest

from app.core.clock import FixedClock
from app.domain.enums import Timeframe
from app.exchanges.binance.constants import (
    EXCHANGE_INFO_PATH,
    KLINES_MAX_LIMIT,
    KLINES_PATH,
    PING_PATH,
    SERVER_TIME_PATH,
    BinanceInterval,
    depth_weight,
    kline_stream_name,
)
from app.exchanges.binance.market_data import (
    TIMEFRAME_TO_INTERVAL,
    BinanceMarketDataAdapter,
    clock_drift_milliseconds,
    milliseconds_to_utc,
    parse_kline,
)
from app.exchanges.binance.rest import BinanceRestClient, WeightBudget
from app.exchanges.errors import (
    ExchangeDataError,
    ExchangeRequestError,
    ExchangeTimeoutError,
    ExchangeUnavailableError,
    IpBannedError,
    RateLimitError,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)

#: A real 15m kline row shape: a 12-element ARRAY whose numbers are STRINGS.
KLINE_ROW: list[Any] = [
    1785240000000,
    "65000.10000000",
    "65250.00000000",
    "64900.00000000",
    "65100.50000000",
    "12.34567000",
    1785240899999,
    "803210.12345678",
    1543,
    "6.10000000",
    "396000.00000000",
    "0",
]

EXCHANGE_INFO_PAYLOAD: dict[str, Any] = {
    "timezone": "UTC",
    "serverTime": 1785240000000,
    "rateLimits": [
        {
            "rateLimitType": "REQUEST_WEIGHT",
            "interval": "MINUTE",
            "intervalNum": 1,
            "limit": 6000,
        },
        {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 100},
    ],
    "exchangeFilters": [],
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "baseAssetPrecision": 8,
            "quoteAsset": "USDT",
            "quoteAssetPrecision": 8,
            "orderTypes": ["LIMIT", "MARKET"],
            "isSpotTradingAllowed": True,
            "isMarginTradingAllowed": True,
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0.01000000",
                    "maxPrice": "1000000.00000000",
                    "tickSize": "0.01000000",
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.00001000",
                    "maxQty": "9000.00000000",
                    "stepSize": "0.00001000",
                },
                {
                    "filterType": "NOTIONAL",
                    "minNotional": "5.00000000",
                    "maxNotional": "9000000.00000000",
                    "applyMinToMarket": True,
                    "applyMaxToMarket": False,
                    "avgPriceMins": 5,
                },
            ],
        },
        {
            "symbol": "ETHUSDT",
            "status": "BREAK",
            "baseAsset": "ETH",
            "quoteAsset": "USDT",
            "isSpotTradingAllowed": True,
            "filters": [],
        },
    ],
}


def build_adapter(
    handler: Any, *, clock: FixedClock | None = None, budget: WeightBudget | None = None
) -> tuple[BinanceMarketDataAdapter, BinanceRestClient]:
    client = BinanceRestClient(
        clock=clock if clock is not None else FixedClock(NOW),
        transport=httpx2.MockTransport(handler),
        budget=budget,
    )
    return BinanceMarketDataAdapter(client), client


def json_response(
    payload: Any, status_code: int = 200, headers: dict[str, str] | None = None
) -> httpx2.Response:
    return httpx2.Response(status_code, json=payload, headers=headers or {})


class TestConnectivity:
    async def test_ping_succeeds_on_an_empty_body(self) -> None:
        adapter, client = build_adapter(lambda request: json_response({}))
        try:
            assert await adapter.ping() is True
        finally:
            await client.aclose()

    async def test_server_time_is_parsed_as_utc(self) -> None:
        adapter, client = build_adapter(
            lambda request: json_response({"serverTime": 1785240000000})
        )
        try:
            assert await adapter.server_time() == datetime(2026, 7, 28, 12, 0, tzinfo=UTC)
        finally:
            await client.aclose()

    async def test_a_missing_server_time_field_is_refused(self) -> None:
        adapter, client = build_adapter(lambda request: json_response({}))
        try:
            with pytest.raises(ExchangeDataError, match="serverTime"):
                await adapter.server_time()
        finally:
            await client.aclose()

    async def test_the_documented_paths_are_the_ones_called(self) -> None:
        seen: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request.url.path)
            return json_response({"serverTime": 1785240000000})

        adapter, client = build_adapter(handler)
        try:
            await adapter.ping()
            await adapter.server_time()
        finally:
            await client.aclose()

        assert seen == [PING_PATH, SERVER_TIME_PATH]


class TestClockDrift:
    """Rule R-22."""

    def test_positive_drift_means_our_clock_is_ahead(self) -> None:
        local = datetime(2026, 7, 28, 12, 0, 2, tzinfo=UTC)
        exchange = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
        assert clock_drift_milliseconds(local, exchange) == Decimal("2000.0")

    def test_negative_drift_means_we_are_behind(self) -> None:
        local = datetime(2026, 7, 28, 12, 0, 0, tzinfo=UTC)
        exchange = datetime(2026, 7, 28, 12, 0, 1, tzinfo=UTC)
        assert clock_drift_milliseconds(local, exchange) == Decimal("-1000.0")

    def test_drift_is_a_decimal_not_a_float(self) -> None:
        drift = clock_drift_milliseconds(NOW, NOW)
        assert isinstance(drift, Decimal)

    def test_naive_timestamps_are_refused(self) -> None:
        with pytest.raises(ValueError, match="timezone-aware"):
            clock_drift_milliseconds(datetime(2026, 7, 28, 12, 0), NOW)  # noqa: DTZ001


class TestExchangeInfo:
    async def test_symbols_and_filters_are_parsed(self) -> None:
        adapter, client = build_adapter(lambda request: json_response(EXCHANGE_INFO_PAYLOAD))
        try:
            info = await adapter.exchange_info()
        finally:
            await client.aclose()

        btc = info.symbols["BTCUSDT"]
        assert btc.is_tradable is True
        assert btc.filters.price is not None
        assert btc.filters.price.tick_size == Decimal("0.01000000")
        assert btc.filters.lot_size is not None
        assert btc.filters.lot_size.step_size == Decimal("0.00001000")
        assert btc.filters.notional is not None
        assert btc.filters.notional.min_notional == Decimal("5.00000000")
        assert btc.filters.is_complete_for_trading is True

    async def test_a_halted_symbol_is_not_tradable(self) -> None:
        """Only TRADING accepts orders. BREAK and HALT do not."""
        adapter, client = build_adapter(lambda request: json_response(EXCHANGE_INFO_PAYLOAD))
        try:
            info = await adapter.exchange_info()
        finally:
            await client.aclose()

        eth = info.symbols["ETHUSDT"]
        assert eth.status == "BREAK"
        assert eth.is_tradable is False

    async def test_a_symbol_without_filters_is_incomplete_not_unlimited(self) -> None:
        """A missing filter means we did not read one, never that there is none."""
        adapter, client = build_adapter(lambda request: json_response(EXCHANGE_INFO_PAYLOAD))
        try:
            info = await adapter.exchange_info()
        finally:
            await client.aclose()

        assert info.symbols["ETHUSDT"].filters.is_complete_for_trading is False

    async def test_the_published_weight_limit_is_readable(self) -> None:
        adapter, client = build_adapter(lambda request: json_response(EXCHANGE_INFO_PAYLOAD))
        try:
            info = await adapter.exchange_info()
        finally:
            await client.aclose()

        assert info.request_weight_limit_per_minute() == 6000

    async def test_requesting_specific_symbols_sends_the_documented_parameter(self) -> None:
        seen: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(str(request.url))
            return json_response(EXCHANGE_INFO_PAYLOAD)

        adapter, client = build_adapter(handler)
        try:
            await adapter.exchange_info(["btcusdt", "ethusdt"])
        finally:
            await client.aclose()

        assert EXCHANGE_INFO_PATH in seen[0]
        assert "BTCUSDT" in seen[0]
        assert "ETHUSDT" in seen[0]

    async def test_a_response_that_is_not_an_object_is_refused(self) -> None:
        adapter, client = build_adapter(lambda request: json_response([1, 2, 3]))
        try:
            with pytest.raises(ExchangeDataError):
                await adapter.exchange_info()
        finally:
            await client.aclose()


class TestKlineParsing:
    def test_a_documented_row_parses_into_exact_decimals(self) -> None:
        candle = parse_kline("BTCUSDT", Timeframe.M15, KLINE_ROW)

        assert candle.open == Decimal("65000.10000000")
        assert candle.high == Decimal("65250.00000000")
        assert candle.low == Decimal("64900.00000000")
        assert candle.close == Decimal("65100.50000000")
        assert candle.volume == Decimal("12.34567000")
        assert candle.quote_volume == Decimal("803210.12345678")
        assert candle.trade_count == 1543

    def test_prices_arrive_as_strings_so_no_float_is_ever_involved(self) -> None:
        candle = parse_kline("BTCUSDT", Timeframe.M15, KLINE_ROW)
        for value in (candle.open, candle.high, candle.low, candle.close):
            assert isinstance(value, Decimal)

    def test_the_open_and_close_times_bracket_the_candle(self) -> None:
        candle = parse_kline("BTCUSDT", Timeframe.M15, KLINE_ROW)
        assert candle.open_time == milliseconds_to_utc(1785240000000)
        assert candle.close_time == milliseconds_to_utc(1785240899999)
        assert candle.close_time > candle.open_time

    def test_a_short_row_is_refused(self) -> None:
        with pytest.raises(ExchangeDataError, match="fields"):
            parse_kline("BTCUSDT", Timeframe.M15, KLINE_ROW[:5])

    def test_a_row_that_is_not_an_array_is_refused(self) -> None:
        with pytest.raises(ExchangeDataError, match="not an array"):
            parse_kline("BTCUSDT", Timeframe.M15, {"open": "1"})


class TestCandleValidation:
    """A malformed candle is rejected, never repaired."""

    def _row_with(self, **overrides: Any) -> list[Any]:
        row = list(KLINE_ROW)
        for index, value in overrides.items():
            row[int(index)] = value
        return row

    def test_a_high_below_the_low_is_refused(self) -> None:
        row = self._row_with(**{"2": "64000.00000000"})  # high below low
        with pytest.raises(ExchangeDataError, match="low"):
            parse_kline("BTCUSDT", Timeframe.M15, row)

    def test_a_close_outside_the_range_is_refused(self) -> None:
        row = self._row_with(**{"4": "70000.00000000"})
        with pytest.raises(ExchangeDataError, match="close"):
            parse_kline("BTCUSDT", Timeframe.M15, row)

    def test_an_open_outside_the_range_is_refused(self) -> None:
        row = self._row_with(**{"1": "10.00000000"})
        with pytest.raises(ExchangeDataError, match="open"):
            parse_kline("BTCUSDT", Timeframe.M15, row)

    def test_a_zero_price_is_refused(self) -> None:
        row = self._row_with(**{"1": "0", "2": "0", "3": "0", "4": "0"})
        with pytest.raises(ExchangeDataError):
            parse_kline("BTCUSDT", Timeframe.M15, row)

    def test_negative_volume_is_refused(self) -> None:
        row = self._row_with(**{"5": "-1.00000000"})
        with pytest.raises(ExchangeDataError, match="volume"):
            parse_kline("BTCUSDT", Timeframe.M15, row)

    def test_a_candle_closing_before_it_opens_is_refused(self) -> None:
        row = self._row_with(**{"6": 1785239000000})
        with pytest.raises(ExchangeDataError, match="closes at or before"):
            parse_kline("BTCUSDT", Timeframe.M15, row)


class TestHistoricalCandles:
    async def test_candles_come_back_parsed(self) -> None:
        adapter, client = build_adapter(lambda request: json_response([KLINE_ROW, KLINE_ROW]))
        try:
            candles = await adapter.historical_candles("btcusdt", Timeframe.M15)
        finally:
            await client.aclose()

        assert len(candles) == 2
        assert candles[0].symbol == "BTCUSDT"
        assert candles[0].timeframe is Timeframe.M15

    async def test_the_request_uses_the_documented_path_and_interval(self) -> None:
        seen: list[httpx2.Request] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append(request)
            return json_response([KLINE_ROW])

        adapter, client = build_adapter(handler)
        try:
            await adapter.historical_candles("BTCUSDT", Timeframe.M15, limit=100)
        finally:
            await client.aclose()

        assert seen[0].url.path == KLINES_PATH
        assert seen[0].url.params["interval"] == BinanceInterval.M15.value
        assert seen[0].url.params["limit"] == "100"

    async def test_a_limit_above_the_documented_maximum_is_refused_locally(self) -> None:
        """Refused before sending, not discovered from an error response."""
        adapter, client = build_adapter(lambda request: json_response([]))
        try:
            with pytest.raises(ValueError, match=str(KLINES_MAX_LIMIT)):
                await adapter.historical_candles(
                    "BTCUSDT", Timeframe.M15, limit=KLINES_MAX_LIMIT + 1
                )
        finally:
            await client.aclose()

    async def test_every_timeframe_we_define_is_mapped_to_an_exchange_interval(
        self,
    ) -> None:
        """The guard against an unmapped timeframe is currently unreachable.

        That is the desired state: every ``Timeframe`` has an interval, so the
        guard cannot fire today. It exists for the day someone adds a timeframe
        and forgets the mapping, and this test is what will fail then - loudly,
        instead of the scanner silently subscribing to nothing.
        """
        assert set(TIMEFRAME_TO_INTERVAL) == set(Timeframe)
        for timeframe, interval in TIMEFRAME_TO_INTERVAL.items():
            assert timeframe.value == interval.value


class TestRateLimitHandling:
    async def test_a_429_becomes_a_rate_limit_error_with_the_retry_delay(self) -> None:
        adapter, client = build_adapter(
            lambda request: json_response(
                {"code": -1003, "msg": "Too much request weight used"},
                status_code=429,
                headers={"Retry-After": "12"},
            )
        )
        try:
            with pytest.raises(RateLimitError) as caught:
                await adapter.ping()
        finally:
            await client.aclose()

        assert caught.value.retry_after_seconds == 12.0
        assert not isinstance(caught.value, IpBannedError)

    async def test_a_418_is_an_ip_ban_and_says_not_to_retry(self) -> None:
        """The documented ban escalates from 2 minutes to 3 days."""
        adapter, client = build_adapter(
            lambda request: json_response(
                {"code": -1003, "msg": "banned until 1785240999999"},
                status_code=418,
                headers={"Retry-After": "120"},
            )
        )
        try:
            with pytest.raises(IpBannedError) as caught:
                await adapter.ping()
        finally:
            await client.aclose()

        assert "Do not retry" in str(caught.value)

    async def test_an_ip_ban_is_also_a_rate_limit_error(self) -> None:
        """So a handler that catches RateLimitError cannot miss it."""
        assert issubclass(IpBannedError, RateLimitError)

    async def test_the_exchange_weight_header_corrects_our_estimate(self) -> None:
        budget = WeightBudget(limit_per_minute=1000)
        adapter, client = build_adapter(
            lambda request: json_response({}, headers={"X-MBX-USED-WEIGHT-1M": "742"}),
            budget=budget,
        )
        try:
            await adapter.ping()
        finally:
            await client.aclose()

        # Our local count was 1; the exchange says 742. The exchange wins.
        assert budget.used == 742

    async def test_a_request_is_refused_before_sending_when_the_budget_is_spent(self) -> None:
        """The whole point: never discover the limit by hitting it."""
        sent: list[str] = []

        def handler(request: httpx2.Request) -> httpx2.Response:
            sent.append(request.url.path)
            return json_response({})

        budget = WeightBudget(limit_per_minute=10)
        adapter, client = build_adapter(handler, budget=budget)
        try:
            for _ in range(8):
                await adapter.ping()
            with pytest.raises(RateLimitError, match="budget exhausted"):
                await adapter.ping()
        finally:
            await client.aclose()

        # 80% of 10 is 8 requests of weight 1. The ninth never left the process.
        assert len(sent) == 8

    async def test_a_400_carries_the_venue_error_code(self) -> None:
        adapter, client = build_adapter(
            lambda request: json_response(
                {"code": -1121, "msg": "Invalid symbol."}, status_code=400
            )
        )
        try:
            with pytest.raises(ExchangeRequestError) as caught:
                await adapter.ping()
        finally:
            await client.aclose()

        assert caught.value.code == -1121
        assert caught.value.status_code == 400

    async def test_a_500_is_reported_as_unavailable(self) -> None:
        adapter, client = build_adapter(
            lambda request: json_response({"msg": "internal"}, status_code=503)
        )
        try:
            with pytest.raises(ExchangeUnavailableError):
                await adapter.ping()
        finally:
            await client.aclose()

    async def test_a_timeout_is_distinguishable_from_a_refusal(self) -> None:
        """A timeout means the outcome is UNKNOWN, which is a different problem."""

        def handler(request: httpx2.Request) -> httpx2.Response:
            raise httpx2.ReadTimeout("timed out", request=request)

        adapter, client = build_adapter(handler)
        try:
            with pytest.raises(ExchangeTimeoutError):
                await adapter.ping()
        finally:
            await client.aclose()

        assert issubclass(ExchangeTimeoutError, ExchangeUnavailableError)


class TestWeightBudget:
    def test_the_window_rolls_after_a_minute(self) -> None:
        budget = WeightBudget(limit_per_minute=100)
        budget.charge(80, now=0.0)
        assert budget.can_afford(10, now=0.0) is False
        assert budget.can_afford(10, now=61.0) is True

    def test_the_published_limit_can_be_adopted(self) -> None:
        budget = WeightBudget(limit_per_minute=1200)
        budget.set_limit(6000)
        assert budget.limit_per_minute == 6000
        assert budget.usable_per_minute == 4800

    def test_a_non_positive_limit_is_refused(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            WeightBudget().set_limit(0)


class TestDocumentedConstants:
    """Guards against a value drifting away from what the documentation says."""

    def test_depth_weight_follows_the_published_table(self) -> None:
        assert depth_weight(100) == 5
        assert depth_weight(500) == 25
        assert depth_weight(1000) == 50
        assert depth_weight(5000) == 250

    def test_the_kline_stream_name_is_lowercase(self) -> None:
        """Stream names are lowercase; REST symbols are uppercase."""
        assert kline_stream_name("BTCUSDT", BinanceInterval.M15) == "btcusdt@kline_15m"

    def test_our_primary_timeframe_maps_to_the_documented_interval(self) -> None:
        assert BinanceInterval.M15.value == "15m"
