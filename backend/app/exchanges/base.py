"""The exchange abstraction.

Strategies, the risk engine, the session engine and the application services
depend on these protocols, never on a concrete venue. Adding Crypto.com later
means adding an implementation, not changing a caller.

Market data and execution are deliberately separate protocols. Reading prices
needs no credentials and carries no risk; submitting an order needs both. A
single fat interface would force a market-data-only component to depend on
methods that can spend money.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.domain.enums import Timeframe
from app.domain.symbol_filters import SymbolFilters


@dataclass(frozen=True, slots=True)
class ExchangeSymbol:
    """One tradable symbol as the exchange describes it."""

    symbol: str
    status: str
    base_asset: str
    quote_asset: str
    is_spot_trading_allowed: bool
    filters: SymbolFilters
    #: The venue's raw entry, kept verbatim for auditing and for fields we do
    #: not model yet. Storing it costs little and avoids guessing later.
    raw: dict[str, Any]

    @property
    def is_tradable(self) -> bool:
        """Only a TRADING symbol accepts orders. HALT and BREAK do not."""
        return self.status == "TRADING" and self.is_spot_trading_allowed


@dataclass(frozen=True, slots=True)
class ExchangeInfo:
    """Exchange metadata: server time, published limits and symbols."""

    server_time: datetime
    symbols: dict[str, ExchangeSymbol]
    #: Published rate limits, verbatim. The client adopts the REQUEST_WEIGHT
    #: ceiling from here rather than assuming one.
    rate_limits: list[dict[str, Any]]

    def request_weight_limit_per_minute(self) -> int | None:
        """The published REQUEST_WEIGHT ceiling per minute, if there is one."""
        for entry in self.rate_limits:
            if entry.get("rateLimitType") != "REQUEST_WEIGHT":
                continue
            if entry.get("interval") != "MINUTE":
                continue
            interval_num = entry.get("intervalNum", 1)
            limit = entry.get("limit")
            if isinstance(limit, int) and isinstance(interval_num, int) and interval_num > 0:
                return limit // interval_num
        return None


@dataclass(frozen=True, slots=True)
class Candle:
    """One completed candle.

    Only closed candles exist as ``Candle`` values. An in-progress candle
    repaints until it closes, and a decision taken on one cannot be reproduced -
    which is exactly the look-ahead bias backtesting is supposed to exclude.
    """

    symbol: str
    timeframe: Timeframe
    open_time: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trade_count: int


@runtime_checkable
class MarketDataAdapter(Protocol):
    """Public market data. Needs no credentials."""

    async def ping(self) -> bool:
        """Whether the venue answers at all."""
        ...

    async def server_time(self) -> datetime:
        """The venue's clock, for drift detection (rule R-22)."""
        ...

    async def exchange_info(self, symbols: list[str] | None = None) -> ExchangeInfo:
        """Symbols, their filters and the published rate limits."""
        ...

    async def historical_candles(
        self,
        symbol: str,
        timeframe: Timeframe,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Candle]:
        """Closed candles, oldest first."""
        ...
