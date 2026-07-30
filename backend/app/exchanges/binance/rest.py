"""Binance Spot REST client for public market data.

No credentials. Every endpoint used here is public, which is why Phase 3 needs
no API keys at all.

Two behaviours matter more than the requests themselves:

* **Weight is budgeted before sending, not counted after.** Binance answers 429
  when the limit is exceeded and 418 when the IP is banned, and the ban escalates
  from 2 minutes to 3 days for repeat offenders. Discovering the limit by hitting
  it is therefore not an option.
* **A 418 is never retried.** Continuing to knock makes the ban longer. It stops
  trading and raises for the operator.
"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Self

import httpx2

from app.core.clock import Clock, SystemClock
from app.core.logging import get_logger
from app.exchanges.binance.constants import (
    HTTP_IP_BANNED,
    HTTP_TOO_MANY_REQUESTS,
    REST_BASE_URL,
    RETRY_AFTER_HEADER,
    USED_WEIGHT_HEADER_PREFIX,
)
from app.exchanges.errors import (
    ExchangeDataError,
    ExchangeRequestError,
    ExchangeTimeoutError,
    ExchangeUnavailableError,
    IpBannedError,
    RateLimitError,
)

logger = get_logger(__name__)

#: Conservative default. The real ceiling is published in exchangeInfo's
#: rateLimits, and is applied by calling `apply_published_limit` once metadata
#: has been read - so the limit comes from the exchange, not from an assumption.
DEFAULT_WEIGHT_BUDGET_PER_MINUTE = 1200

#: Stop before the published ceiling. The counter is the exchange's, ours is an
#: estimate, and the gap between them is the margin that avoids a ban.
WEIGHT_SAFETY_FRACTION = 0.8


class WeightBudget:
    """Tracks request weight used inside the current minute.

    Local accounting, corrected by the exchange's own header on every response.
    The local figure exists so a request can be refused *before* being sent.
    """

    def __init__(self, limit_per_minute: int = DEFAULT_WEIGHT_BUDGET_PER_MINUTE) -> None:
        self._limit = limit_per_minute
        self._used = 0
        self._window_started_at = 0.0

    @property
    def limit_per_minute(self) -> int:
        return self._limit

    @property
    def used(self) -> int:
        return self._used

    @property
    def usable_per_minute(self) -> int:
        return int(self._limit * WEIGHT_SAFETY_FRACTION)

    def set_limit(self, limit_per_minute: int) -> None:
        """Adopt the ceiling the exchange published for itself."""
        if limit_per_minute <= 0:
            raise ValueError("Weight limit must be positive")
        self._limit = limit_per_minute

    def _roll_window(self, now: float) -> None:
        if now - self._window_started_at >= 60:
            self._window_started_at = now
            self._used = 0

    def can_afford(self, weight: int, now: float) -> bool:
        self._roll_window(now)
        return self._used + weight <= self.usable_per_minute

    def charge(self, weight: int, now: float) -> None:
        self._roll_window(now)
        self._used += weight

    def observe_reported_usage(self, reported: int) -> None:
        """Trust the exchange's counter over ours when it is higher.

        Our estimate can only be too low - a retry, a redirect or another
        process sharing the IP all consume weight we never counted.
        """
        self._used = max(self._used, reported)


class BinanceRestClient:
    """Thin, public-only HTTP client with weight budgeting."""

    def __init__(
        self,
        *,
        base_url: str = REST_BASE_URL,
        timeout_seconds: float = 10.0,
        clock: Clock | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
        budget: WeightBudget | None = None,
    ) -> None:
        self._clock = clock if clock is not None else SystemClock()
        self._budget = budget if budget is not None else WeightBudget()
        self._client = httpx2.AsyncClient(
            base_url=base_url,
            timeout=httpx2.Timeout(timeout_seconds),
            transport=transport,
            headers={"Accept": "application/json"},
        )

    @property
    def budget(self) -> WeightBudget:
        return self._budget

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------- request ---

    async def get(self, path: str, *, weight: int, params: dict[str, Any] | None = None) -> Any:
        """Perform one weighted GET.

        Raises rather than waiting when the budget is exhausted: the caller
        decides whether to back off or to abandon, and a scheduler silently
        sleeping inside a risk check would be worse than an explicit refusal.
        """
        now = self._clock.now().timestamp()
        if not self._budget.can_afford(weight, now):
            raise RateLimitError(
                f"Local weight budget exhausted: {self._budget.used}/"
                f"{self._budget.usable_per_minute} used this minute, "
                f"{weight} more requested."
            )
        self._budget.charge(weight, now)

        try:
            response = await self._client.get(path, params=params)
        except httpx2.TimeoutException as exc:
            raise ExchangeTimeoutError(f"Timed out calling {path}") from exc
        except httpx2.TransportError as exc:
            raise ExchangeUnavailableError(f"Transport failure calling {path}") from exc

        self._observe_weight_headers(response)
        self._raise_for_status(path, response)

        try:
            return response.json()
        except ValueError as exc:
            raise ExchangeDataError(f"{path} returned a body that is not JSON") from exc

    def _observe_weight_headers(self, response: httpx2.Response) -> None:
        for name, value in response.headers.items():
            if not name.lower().startswith(USED_WEIGHT_HEADER_PREFIX):
                continue
            try:
                self._budget.observe_reported_usage(int(value))
            except ValueError:
                # A header we cannot parse is not worth failing a request over,
                # but it must be visible: our budgeting is running blind.
                logger.warning("unparsable_weight_header", header=name)

    def _raise_for_status(self, path: str, response: httpx2.Response) -> None:
        if response.status_code < 400:
            return

        retry_after = self._retry_after(response)
        code, message = self._error_body(response)

        if response.status_code == HTTP_IP_BANNED:
            # Never retried. Knocking again extends the ban, which is documented
            # to escalate from 2 minutes to 3 days.
            raise IpBannedError(
                f"IP banned by the exchange calling {path}: {message}. "
                f"Do not retry; trading must stop until it clears.",
                retry_after_seconds=retry_after,
            )

        if response.status_code == HTTP_TOO_MANY_REQUESTS:
            raise RateLimitError(
                f"Rate limited calling {path}: {message}",
                retry_after_seconds=retry_after,
            )

        if response.status_code >= 500:
            raise ExchangeUnavailableError(f"{path} returned {response.status_code}: {message}")

        raise ExchangeRequestError(
            f"{path} rejected: {message}", code=code, status_code=response.status_code
        )

    @staticmethod
    def _retry_after(response: httpx2.Response) -> float | None:
        raw = response.headers.get(RETRY_AFTER_HEADER)
        if raw is None:
            return None
        try:
            return float(raw)
        except ValueError:
            return None

    @staticmethod
    def _error_body(response: httpx2.Response) -> tuple[int | None, str]:
        """Binance errors look like {"code": -1121, "msg": "Invalid symbol."}."""
        try:
            payload = response.json()
        except ValueError:
            return None, response.text[:200]
        if not isinstance(payload, dict):
            return None, str(payload)[:200]
        code = payload.get("code")
        return (code if isinstance(code, int) else None), str(payload.get("msg", ""))


async def sleep_for_retry_after(error: RateLimitError, *, maximum_seconds: float = 60.0) -> None:
    """Wait as instructed by a 429, capped.

    Never used for :class:`IpBannedError`: that one is escalated to the operator
    rather than waited out.
    """
    if isinstance(error, IpBannedError):
        raise error
    delay = error.retry_after_seconds if error.retry_after_seconds is not None else 1.0
    await asyncio.sleep(min(delay, maximum_seconds))
