"""Candle window tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain.candle_series import CandleSeriesError
from app.domain.candle_window import CandleWindow, CandleWindowError
from app.domain.enums import Timeframe

M15 = Timeframe.M15
BASE = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class FakeCandle:
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


def fake_candle(index: int, close: int = 100) -> FakeCandle:
    return FakeCandle(
        open_time=BASE + M15.duration * index,
        open=Decimal(close),
        high=Decimal(close + 2),
        low=Decimal(close - 2),
        close=Decimal(close),
        volume=Decimal(10),
    )


def window(count: int) -> CandleWindow:
    return CandleWindow.from_candles(
        "BTCUSDT", M15, [fake_candle(index, 100 + index) for index in range(count)]
    )


class TestConstruction:
    def test_the_real_exchange_candle_satisfies_the_protocol(self) -> None:
        """The protocol exists to accept the candle types this application
        actually has. Every one of them is a frozen dataclass, which a protocol
        declaring mutable attributes would silently reject."""
        from app.exchanges.base import Candle as ExchangeCandle

        candle = ExchangeCandle(
            symbol="BTCUSDT",
            timeframe=M15,
            open_time=BASE,
            close_time=BASE + M15.duration,
            open=Decimal(100),
            high=Decimal(102),
            low=Decimal(98),
            close=Decimal(101),
            volume=Decimal(5),
            quote_volume=Decimal(500),
            trade_count=10,
        )
        built = CandleWindow.from_candles("BTCUSDT", M15, [candle])
        assert built.last_close == Decimal(101)

    def test_a_window_is_built_from_anything_candle_shaped(self) -> None:
        """Structural, so the same window can come from the live feed, the
        database or a backtest fixture - and a strategy cannot tell which."""
        result = window(3)
        assert len(result) == 3
        assert result.closes == (Decimal(100), Decimal(101), Decimal(102))

    def test_an_empty_window_is_valid(self) -> None:
        empty = CandleWindow.empty("BTCUSDT", M15)
        assert empty.is_empty is True
        assert len(empty) == 0

    def test_series_of_different_lengths_are_refused(self) -> None:
        with pytest.raises(CandleWindowError, match="different lengths"):
            CandleWindow(
                symbol="BTCUSDT",
                timeframe=M15,
                open_times=(BASE,),
                opens=(Decimal(1), Decimal(2)),
                highs=(Decimal(1),),
                lows=(Decimal(1),),
                closes=(Decimal(1),),
                volumes=(Decimal(1),),
            )

    def test_a_hole_in_the_series_is_refused(self) -> None:
        """Fatal here, merely reportable in storage. Storage records a gap and
        carries on; a decision window cannot, because an indicator computed
        across a hole is computed over a period that never existed."""
        candles = [fake_candle(0), fake_candle(1), fake_candle(5)]
        with pytest.raises(CandleWindowError, match="not contiguous"):
            CandleWindow.from_candles("BTCUSDT", M15, candles)

    def test_the_gap_is_named_in_the_error(self) -> None:
        """An operator reading the log must not have to diff the series to find
        out which candles are missing."""
        candles = [fake_candle(0), fake_candle(3)]
        with pytest.raises(CandleWindowError, match="2 missing"):
            CandleWindow.from_candles("BTCUSDT", M15, candles)

    def test_candles_out_of_order_are_refused(self) -> None:
        candles = [fake_candle(2), fake_candle(1), fake_candle(0)]
        with pytest.raises(CandleSeriesError, match="out of order"):
            CandleWindow.from_candles("BTCUSDT", M15, candles)

    def test_a_misaligned_candle_is_refused(self) -> None:
        odd = FakeCandle(
            open_time=BASE + timedelta(minutes=7),
            open=Decimal(1),
            high=Decimal(1),
            low=Decimal(1),
            close=Decimal(1),
            volume=Decimal(1),
        )
        with pytest.raises(CandleSeriesError, match="not aligned"):
            CandleWindow.from_candles("BTCUSDT", M15, [odd])

    def test_a_high_below_its_low_is_refused(self) -> None:
        broken = FakeCandle(
            open_time=BASE,
            open=Decimal(10),
            high=Decimal(5),
            low=Decimal(9),
            close=Decimal(10),
            volume=Decimal(1),
        )
        with pytest.raises(CandleWindowError, match="high below its low"):
            CandleWindow.from_candles("BTCUSDT", M15, [broken])

    def test_a_window_is_immutable(self) -> None:
        """Two strategies evaluated on the same window must see the same data."""
        result = window(3)
        with pytest.raises(AttributeError):
            result.closes = ()  # type: ignore[misc]


class TestInspection:
    def test_the_last_candle_is_the_newest(self) -> None:
        result = window(5)
        assert result.last_open_time == BASE + M15.duration * 4
        assert result.last_close == Decimal(104)

    def test_an_empty_window_has_no_last_candle(self) -> None:
        empty = CandleWindow.empty("BTCUSDT", M15)
        with pytest.raises(CandleWindowError, match="no last candle"):
            _ = empty.last_close

    def test_has_at_least_answers_the_warm_up_question(self) -> None:
        result = window(10)
        assert result.has_at_least(10) is True
        assert result.has_at_least(11) is False

    def test_a_named_series_is_returned(self) -> None:
        result = window(3)
        assert result.series("close") == (Decimal(100), Decimal(101), Decimal(102))
        assert result.series("volume") == (Decimal(10), Decimal(10), Decimal(10))

    def test_an_unknown_series_name_is_refused(self) -> None:
        with pytest.raises(CandleWindowError, match="Unknown series"):
            window(3).series("typical_price")


class TestTail:
    def test_the_tail_keeps_the_most_recent_candles(self) -> None:
        """Slicing from the end: the oldest candle is the droppable one."""
        result = window(10).tail(3)
        assert len(result) == 3
        assert result.last_open_time == BASE + M15.duration * 9
        assert result.closes == (Decimal(107), Decimal(108), Decimal(109))

    def test_a_tail_longer_than_the_window_returns_everything(self) -> None:
        assert len(window(3).tail(10)) == 3

    def test_a_zero_length_tail_is_empty(self) -> None:
        assert window(5).tail(0).is_empty is True

    def test_a_negative_tail_is_refused(self) -> None:
        with pytest.raises(CandleWindowError, match="cannot be negative"):
            window(5).tail(-1)

    def test_the_tail_is_still_a_valid_window(self) -> None:
        tail = window(10).tail(4)
        assert tail.symbol == "BTCUSDT"
        assert tail.timeframe is M15
