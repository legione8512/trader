"""Exchange adapter errors.

Deliberately exchange-agnostic: the execution layer reacts to *what happened*,
not to which venue said it. Adding Crypto.com later must not require changing
any handler.
"""

from __future__ import annotations


class ExchangeError(Exception):
    """Base class for anything an exchange adapter reports."""


class ExchangeUnavailableError(ExchangeError):
    """The exchange could not be reached, or answered with a server error.

    Transient by nature. The safe response is to stop opening new positions
    until it clears, never to assume an order did or did not happen.
    """


class ExchangeTimeoutError(ExchangeUnavailableError):
    """A request timed out with the outcome genuinely unknown.

    The single most dangerous condition in the system. An order request that
    times out MUST lead to reconciliation, never to a second submission.
    """


class RateLimitError(ExchangeError):
    """HTTP 429: the request rate or weight limit was exceeded."""

    def __init__(self, message: str, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class IpBannedError(RateLimitError):
    """HTTP 418: the IP has been banned for repeated rate limit violations.

    The documented ban escalates from 2 minutes to 3 days for repeat offenders,
    so this is never retried automatically. It stops trading and alerts the
    operator: continuing to knock makes the ban longer.
    """


class ExchangeRequestError(ExchangeError):
    """The exchange rejected the request as invalid.

    Carries the venue's own error code so a caller can distinguish, for example,
    an unknown symbol from a filter violation.
    """

    def __init__(self, message: str, code: int | None = None, status_code: int | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class ExchangeDataError(ExchangeError):
    """The exchange answered successfully with data we cannot trust.

    A malformed candle is not a parsing inconvenience: acting on it would mean
    trading on numbers nobody verified. Rejecting is the only safe option.
    """
