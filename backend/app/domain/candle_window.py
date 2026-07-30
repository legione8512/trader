"""The candle history a strategy is allowed to see.

A window is columnar - separate tuples for opens, highs, lows, closes and
volumes - because that is exactly what the indicators consume. Handing a
strategy a list of candle objects would force every strategy to unpack them the
same way, and the first one to unpack them slightly differently would be
computing something else.

It is deliberately a *value*: frozen, validated once, and knowing nothing about
where the candles came from. The same window can be built from the live feed, a
database range or a backtest fixture, and a strategy cannot tell which - which
is precisely what makes a backtest mean anything.

**Only closed candles belong in a window.** Nothing here can check that on its
own, because a closed candle and an in-progress one look identical once the
numbers are extracted; the guarantee is upheld upstream, at ingestion, where the
information still exists.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.domain.candle_series import find_gaps, validate_ordering
from app.domain.enums import Timeframe
from app.domain.errors import DomainError


class CandleWindowError(DomainError):
    """The window violates an invariant a strategy depends on."""


@runtime_checkable
class CandleLike(Protocol):
    """Anything shaped like a candle. Structural, so no import is needed.

    Declared as read-only properties rather than attributes, and that is not a
    stylistic choice: a Protocol with mutable attributes is not satisfied by a
    frozen dataclass, and every candle type in this application is frozen. A
    window only ever reads, so read-only is also the honest declaration.
    """

    @property
    def open_time(self) -> datetime: ...

    @property
    def open(self) -> Decimal: ...

    @property
    def high(self) -> Decimal: ...

    @property
    def low(self) -> Decimal: ...

    @property
    def close(self) -> Decimal: ...

    @property
    def volume(self) -> Decimal: ...


@dataclass(frozen=True, slots=True)
class CandleWindow:
    """A contiguous, ordered run of closed candles for one symbol."""

    symbol: str
    timeframe: Timeframe
    open_times: tuple[datetime, ...]
    opens: tuple[Decimal, ...]
    highs: tuple[Decimal, ...]
    lows: tuple[Decimal, ...]
    closes: tuple[Decimal, ...]
    volumes: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        lengths = {
            len(self.open_times),
            len(self.opens),
            len(self.highs),
            len(self.lows),
            len(self.closes),
            len(self.volumes),
        }
        if len(lengths) != 1:
            raise CandleWindowError(f"Window series have different lengths: {sorted(lengths)}")

        # Ordering and grid alignment are checked here rather than trusted.
        validate_ordering(self.open_times, self.timeframe)

        # Contiguity is checked as well, and separately, because it is the one
        # invariant that is fatal HERE and merely reportable elsewhere. Storage
        # records a gap and carries on; a decision window cannot, because an
        # indicator computed across a hole is computed over a period that never
        # existed, and nothing downstream could detect it afterwards.
        gaps = find_gaps(self.open_times, self.timeframe)
        if gaps:
            raise CandleWindowError(
                f"{self.symbol} {self.timeframe.value} window is not contiguous: {gaps[0]}"
            )

        for index in range(len(self.open_times)):
            if self.highs[index] < self.lows[index]:
                raise CandleWindowError(
                    f"Candle {self.open_times[index].isoformat()} has a high below its low"
                )

    # ----------------------------------------------------------- construction ---

    @classmethod
    def from_candles(
        cls, symbol: str, timeframe: Timeframe, candles: Iterable[CandleLike]
    ) -> CandleWindow:
        """Build a window from anything candle-shaped, oldest first."""
        rows = list(candles)
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            open_times=tuple(row.open_time for row in rows),
            opens=tuple(row.open for row in rows),
            highs=tuple(row.high for row in rows),
            lows=tuple(row.low for row in rows),
            closes=tuple(row.close for row in rows),
            volumes=tuple(row.volume for row in rows),
        )

    @classmethod
    def empty(cls, symbol: str, timeframe: Timeframe) -> CandleWindow:
        return cls(symbol, timeframe, (), (), (), (), (), ())

    # ------------------------------------------------------------- inspection ---

    def __len__(self) -> int:
        return len(self.open_times)

    @property
    def is_empty(self) -> bool:
        return not self.open_times

    @property
    def last_open_time(self) -> datetime:
        if self.is_empty:
            raise CandleWindowError("An empty window has no last candle")
        return self.open_times[-1]

    @property
    def last_close(self) -> Decimal:
        if self.is_empty:
            raise CandleWindowError("An empty window has no last candle")
        return self.closes[-1]

    def has_at_least(self, count: int) -> bool:
        """Whether the window is long enough for an indicator to be warm.

        Checked before evaluating, not after: a strategy that acts on a
        half-warm indicator is acting on a different indicator.
        """
        return len(self.open_times) >= count

    def tail(self, count: int) -> CandleWindow:
        """The most recent ``count`` candles.

        Slicing from the end, never from the start: the oldest candle is the one
        that may be dropped, and dropping from the recent end would be dropping
        the candles the decision is about.
        """
        if count < 0:
            raise CandleWindowError(f"Tail length cannot be negative: {count}")
        if count == 0:
            return CandleWindow.empty(self.symbol, self.timeframe)
        return CandleWindow(
            symbol=self.symbol,
            timeframe=self.timeframe,
            open_times=self.open_times[-count:],
            opens=self.opens[-count:],
            highs=self.highs[-count:],
            lows=self.lows[-count:],
            closes=self.closes[-count:],
            volumes=self.volumes[-count:],
        )

    def series(self, name: str) -> Sequence[Decimal]:
        """One named column, for an indicator that takes a single series."""
        columns: dict[str, tuple[Decimal, ...]] = {
            "open": self.opens,
            "high": self.highs,
            "low": self.lows,
            "close": self.closes,
            "volume": self.volumes,
        }
        if name not in columns:
            raise CandleWindowError(f"Unknown series {name!r}, expected one of {sorted(columns)}")
        return columns[name]

    def __repr__(self) -> str:
        if self.is_empty:
            return f"<CandleWindow {self.symbol} {self.timeframe.value} empty>"
        return (
            f"<CandleWindow {self.symbol} {self.timeframe.value} "
            f"{len(self)} candles ending {self.last_open_time.isoformat()}>"
        )
