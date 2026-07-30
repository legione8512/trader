"""Market data freshness, rule R-09."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.config.settings import Settings
from app.core.clock import FixedClock
from app.domain.enums import HealthStatus, Timeframe
from app.market_data.freshness import (
    FeedFreshnessMonitor,
    FeedKey,
    build_market_data_check,
)
from app.monitoring.health import build_default_registry
from app.persistence.database import build_engine

M15 = Timeframe.M15
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=UTC)


def monitor_at(now: datetime = NOW, **kwargs: int) -> FeedFreshnessMonitor:
    return FeedFreshnessMonitor(clock=FixedClock(now), **kwargs)


class TestFreshness:
    def test_a_feed_that_just_delivered_is_healthy(self) -> None:
        monitor = monitor_at()
        monitor.record_candle("BTCUSDT", M15, NOW - timedelta(minutes=1))
        assert monitor.status() is HealthStatus.HEALTHY

    def test_one_interval_behind_is_still_healthy(self) -> None:
        """The next candle is legitimately still forming."""
        monitor = monitor_at()
        monitor.record_candle("BTCUSDT", M15, NOW - timedelta(minutes=14))
        assert monitor.status() is HealthStatus.HEALTHY

    def test_more_than_two_intervals_behind_is_degraded(self) -> None:
        """R-09: reject new orders. Not UNHEALTHY - an open position still
        needs managing, and the service itself is answering fine."""
        monitor = monitor_at()
        monitor.record_candle("BTCUSDT", M15, NOW - timedelta(minutes=31))

        assert monitor.status() is HealthStatus.DEGRADED
        assert monitor.status().blocks_new_positions is True

    def test_a_feed_that_never_delivered_is_starting_not_healthy(self) -> None:
        """ "No candle has arrived yet" is not "everything is fine"."""
        monitor = monitor_at()
        monitor.expect("BTCUSDT", M15)

        report = monitor.report()
        assert len(report) == 1
        assert report[0].has_data is False
        assert monitor.status() is HealthStatus.STARTING

    def test_an_empty_monitor_never_reports_healthy(self) -> None:
        """A system watching nothing must not claim its market data is fine."""
        assert monitor_at().status() is not HealthStatus.HEALTHY

    def test_the_worst_feed_decides_the_overall_status(self) -> None:
        monitor = monitor_at()
        monitor.record_candle("BTCUSDT", M15, NOW - timedelta(minutes=1))
        monitor.record_candle("ETHUSDT", M15, NOW - timedelta(hours=3))

        assert monitor.status() is HealthStatus.DEGRADED

    def test_the_threshold_scales_with_the_timeframe(self) -> None:
        """Two hours behind is routine on 4h candles and catastrophic on 15m."""
        monitor = monitor_at()
        monitor.record_candle("BTCUSDT", M15, NOW - timedelta(hours=2))
        monitor.record_candle("ETHUSDT", Timeframe.H4, NOW - timedelta(hours=2))

        by_key = {entry.key: entry for entry in monitor.report()}
        assert by_key[FeedKey("BTCUSDT", M15)].is_stale is True
        assert by_key[FeedKey("ETHUSDT", Timeframe.H4)].is_stale is False

    def test_freshness_never_moves_backwards(self) -> None:
        """A gap-fill delivering older candles must not make the feed look
        fresher, and an out-of-order arrival must not make it look staler."""
        monitor = monitor_at()
        monitor.record_candle("BTCUSDT", M15, NOW - timedelta(minutes=1))
        monitor.record_candle("BTCUSDT", M15, NOW - timedelta(hours=5))

        assert monitor.status() is HealthStatus.HEALTHY

    def test_symbols_are_matched_case_insensitively(self) -> None:
        """Stream names are lowercase, REST symbols uppercase; they meet here."""
        monitor = monitor_at()
        monitor.expect("btcusdt", M15)
        monitor.record_candle("BTCUSDT", M15, NOW)

        assert len(monitor.report()) == 1

    def test_a_naive_close_time_is_refused(self) -> None:
        monitor = monitor_at()
        with pytest.raises(ValueError, match="timezone-aware"):
            monitor.record_candle("BTCUSDT", M15, datetime(2026, 7, 28, 12, 0))  # noqa: DTZ001

    def test_a_tolerance_below_one_interval_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one interval"):
            FeedFreshnessMonitor(tolerance_intervals=0)

    def test_the_tolerance_is_configurable(self) -> None:
        monitor = monitor_at(tolerance_intervals=8)
        monitor.record_candle("BTCUSDT", M15, NOW - timedelta(hours=1))
        assert monitor.status() is HealthStatus.HEALTHY


class TestHealthCheck:
    async def test_the_check_reports_the_age_of_each_feed(self) -> None:
        monitor = monitor_at()
        monitor.record_candle("BTCUSDT", M15, NOW - timedelta(minutes=5))
        check = build_market_data_check(monitor)

        result = await check()

        assert result.name == "market_data"
        assert result.status is HealthStatus.HEALTHY
        assert result.detail is not None
        assert "BTCUSDT@15m=300s" in result.detail

    async def test_the_check_names_a_feed_with_no_data(self) -> None:
        monitor = monitor_at()
        monitor.expect("BTCUSDT", M15)
        check = build_market_data_check(monitor)

        result = await check()

        assert result.status is HealthStatus.STARTING
        assert result.detail is not None
        assert "no data" in result.detail

    async def test_a_stale_feed_reaches_the_operator_through_the_registry(
        self, settings: Settings
    ) -> None:
        """The point of the whole module: a stale feed must show up on the same
        endpoint the operator already watches."""
        monitor = monitor_at()
        monitor.record_candle("BTCUSDT", M15, NOW - timedelta(hours=2))
        engine = build_engine(settings)
        try:
            registry = build_default_registry(
                settings,
                engine,
                extra_checks={"market_data": build_market_data_check(monitor)},
            )
            assert "market_data" in registry.names
            results = {result.name: result for result in await registry.run_all()}
        finally:
            await engine.dispose()

        assert results["market_data"].status is HealthStatus.DEGRADED

    def test_the_check_is_not_registered_before_a_feed_runs(self, settings: Settings) -> None:
        """Nothing ingests yet. Reporting "no market data" as a permanent state
        of the deployment would train the operator to ignore it."""
        engine = build_engine(settings)
        registry = build_default_registry(settings, engine)
        assert "market_data" not in registry.names
