"""Command line entry points.

Usage:

    python -m app.cli seed
    python -m app.cli show-config
    python -m app.cli backfill --days 730
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.backtest.engine import BacktestConfig, MarketAssumptions
from app.backtest.runner import format_segment, load_window, run_segment, split_in_sample
from app.config.settings import get_settings
from app.core.logging import configure_logging, get_logger, register_configured_secrets
from app.domain.enums import Timeframe
from app.domain.risk.economics import TradingCosts
from app.domain.risk.limits import RiskLimits
from app.domain.symbol_filters import (
    LotSizeFilter,
    NotionalFilter,
    PriceFilter,
    SymbolFilters,
)
from app.exchanges.binance.market_data import BinanceMarketDataAdapter
from app.exchanges.binance.rest import BinanceRestClient
from app.market_data.history import HistoryDownloader
from app.persistence.candles import CandleRepository
from app.persistence.database import build_engine, build_session_factory
from app.persistence.repositories import ConfigurationRepository, ExchangeRepository
from app.persistence.seed import BINANCE_CODE, seed
from app.strategies.trend_pullback import TrendPullbackStrategy

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Backtest parameters
# ---------------------------------------------------------------------------
#
# The market-quality gates are set WIDE here, and that is a stated choice
# rather than an oversight. R-11, R-12 and R-15 judge live execution quality
# against measurements a candle does not contain; leaving them uncalibrated
# would refuse every trade and produce an empty run that says nothing about
# whether the strategy has an edge. They are calibrated against real
# measurements in Phase 8, not guessed from history here.
#
# R-14 is NOT widened. The minimum reward-to-risk is the gate that decides
# whether a trade is worth taking after costs, and it binds.

BACKTEST_LIMITS = RiskLimits(
    reference_capital=Decimal("1000.00"),
    max_candle_age_seconds=1800,
    max_signal_age_seconds=900,
    max_spread_bps=Decimal("100"),
    min_order_book_depth_quote=Decimal("1"),
    min_atr_percent=Decimal("0.01"),
    max_atr_percent=Decimal("50.00"),
    min_reward_risk_ratio=Decimal("1.8"),
    max_estimated_slippage_bps=Decimal("100"),
    max_clock_drift_ms=1000,
)

#: Binance spot with the BNB discount (decision OD-16), verified from the
#: published schedule: 0.075% per side.
BACKTEST_COSTS = TradingCosts(
    fee_rate_per_side=Decimal("0.00075"), estimated_slippage_bps=Decimal("2")
)

BACKTEST_MARKET = MarketAssumptions(
    spread_bps=Decimal("2"),
    order_book_depth_quote=Decimal("500000"),
    slippage_bps=Decimal("2"),
)

#: Held constant for the whole run. RON/USDT moved over two years, which is why
#: every headline figure is reported in R as well.
BACKTEST_FUNDING_RATE = Decimal("4.60")


def default_filters_for(symbol: str) -> SymbolFilters:
    """Filters used for backtesting.

    Deliberately approximate and deliberately not fetched live: a two-year
    replay would otherwise be judged against today's filters, which is its own
    small lie. What matters for the result is the minimum notional and the lot
    step, both of which have been stable for these symbols.
    """
    return SymbolFilters(
        symbol=symbol,
        price=PriceFilter(
            min_price=Decimal("0.01"),
            max_price=Decimal("10000000"),
            tick_size=Decimal("0.01"),
        ),
        lot_size=LotSizeFilter(
            min_quantity=Decimal("0.00001"),
            max_quantity=Decimal("9000"),
            step_size=Decimal("0.00001"),
        ),
        notional=NotionalFilter(
            min_notional=Decimal("5"),
            max_notional=Decimal("9000000"),
            applies_to_market_orders=True,
        ),
    )


async def _seed() -> int:
    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            result = await seed(session, settings)
            await session.commit()

        logger.info(
            "seed_completed",
            exchange_created=result.exchange_created,
            pairs_created=list(result.pairs_created),
            risk_configuration_version=result.risk_configuration_version,
            trading_configuration_version=result.trading_configuration_version,
            changed_anything=result.changed_anything,
        )
    finally:
        await engine.dispose()
    return 0


async def _show_config() -> int:
    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            repository = ConfigurationRepository(session)
            risk = await repository.get_active_risk_configuration()
            trading = await repository.get_active_trading_configuration()

        if risk is None or trading is None:
            logger.error("no_active_configuration", hint="Run: python -m app.cli seed")
            return 1

        logger.info(
            "active_configuration",
            risk_version=risk.version,
            reference_capital_ron=str(risk.reference_capital_ron),
            maximum_risk_per_trade_percent=str(risk.maximum_risk_per_trade_percent),
            maximum_trades_per_day=risk.maximum_trades_per_day,
            maximum_consecutive_losses=risk.maximum_consecutive_losses,
            maximum_open_positions=risk.maximum_open_positions,
            daily_pnl_basis=risk.daily_pnl_basis.value,
            daily_profit_giveback_percent=(
                None
                if risk.daily_profit_giveback_percent is None
                else str(risk.daily_profit_giveback_percent)
            ),
            trading_version=trading.version,
            autonomy_mode=trading.autonomy_mode.value,
            emergency_stop_active=trading.emergency_stop_active,
        )
    finally:
        await engine.dispose()
    return 0


async def _backfill(days: int, timeframe: Timeframe, symbols: list[str] | None) -> int:
    """Download historical candles.

    Public market data, so no credentials are involved and none are read. That
    is worth being explicit about: a long download is exactly the job someone
    would be tempted to run with keys attached.
    """
    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    client = BinanceRestClient()
    try:
        async with session_factory() as session:
            exchange = await ExchangeRepository(session).get_by_code(BINANCE_CODE)
            if exchange is None:
                logger.error("no_exchange_row", hint="Run: python -m app.cli seed")
                return 1

            pairs = await ExchangeRepository(session).list_enabled_pairs(exchange.id)
            wanted = {symbol.upper() for symbol in symbols} if symbols else None
            selected = [
                (pair.symbol, pair.id)
                for pair in pairs
                if wanted is None or pair.symbol.upper() in wanted
            ]
            if not selected:
                # Pairs are disabled by default; enabling one is an operator
                # decision, so an empty list means a decision was not taken
                # rather than that something is broken.
                logger.error(
                    "no_pairs_selected",
                    hint="No enabled trading pair matched. Enable a pair first.",
                    requested=symbols,
                )
                return 1

            downloader = HistoryDownloader(
                BinanceMarketDataAdapter(client), CandleRepository(session)
            )
            report = await downloader.download(
                pairs=selected,
                timeframe=timeframe,
                start=datetime.now(UTC) - timedelta(days=days),
            )
            await session.commit()

        for entry in report.symbols:
            logger.info(
                "backfill_symbol",
                symbol=entry.symbol,
                inserted=entry.candles_inserted,
                already_present=entry.candles_already_present,
                stored_total=entry.stored_total,
                stored_from=entry.stored_from.isoformat() if entry.stored_from else None,
                stored_to=entry.stored_to.isoformat() if entry.stored_to else None,
                gaps=len(entry.gaps),
                complete=entry.is_complete,
            )
        if report.aborted_reason is not None:
            logger.error("backfill_aborted", reason=report.aborted_reason)
            return 1
        if not report.is_usable_for_backtesting:
            # Not a failure of the download. A hole the exchange does not have
            # is a fact about the data, and a backtest over it must know.
            logger.warning(
                "backfill_incomplete",
                detail="Stored history has gaps. Backtests over it will be refused.",
            )
        logger.info("backfill_completed", total_inserted=report.total_inserted)
    finally:
        await client.aclose()
        await engine.dispose()
    return 0


async def _backtest(symbol: str, timeframe: Timeframe, days: int) -> int:
    """Replay the baseline strategy over stored candles.

    The development segment is reported first and the held-out segment second,
    in that order and only once. The split is chronological and its size is
    fixed in code: a split anyone can move is a split someone will move.
    """
    settings = get_settings()
    engine = build_engine(settings)
    session_factory = build_session_factory(engine)
    try:
        async with session_factory() as session:
            exchange = await ExchangeRepository(session).get_by_code(BINANCE_CODE)
            if exchange is None:
                logger.error("no_exchange_row", hint="Run: python -m app.cli seed")
                return 1
            pair = await ExchangeRepository(session).get_pair(exchange.id, symbol.upper())
            if pair is None:
                logger.error("unknown_pair", symbol=symbol)
                return 1

            end = datetime.now(UTC)
            window = await load_window(
                session,
                trading_pair_id=pair.id,
                symbol=pair.symbol,
                timeframe=timeframe,
                start=end - timedelta(days=days),
                end=end,
            )

        strategy = TrendPullbackStrategy()
        config = BacktestConfig(
            symbol=pair.symbol,
            timeframe=timeframe,
            limits=BACKTEST_LIMITS,
            costs=BACKTEST_COSTS,
            filters=default_filters_for(pair.symbol),
            funding_rate=BACKTEST_FUNDING_RATE,
            market=BACKTEST_MARKET,
        )

        development, held_out = split_in_sample(window)
        print(format_segment(run_segment(strategy, development, config, "DEVELOPMENT")))  # noqa: T201
        print()  # noqa: T201
        print(format_segment(run_segment(strategy, held_out, config, "HELD OUT")))  # noqa: T201
        print()  # noqa: T201
        print(  # noqa: T201
            "Assumptions this run depends on:\n"
            + "\n".join(
                f"  {group}.{key}: {value}"
                for group, entries in run_segment(
                    strategy, held_out, config, "x"
                ).result.assumptions.items()
                for key, value in entries.items()
            )
        )
    finally:
        await engine.dispose()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Trader maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed", help="Create initial reference data and configuration")
    subparsers.add_parser("show-config", help="Print the active configuration")

    backfill = subparsers.add_parser(
        "backfill", help="Download historical candles (public data, no credentials)"
    )
    backfill.add_argument(
        "--days", type=int, default=730, help="How far back to download (default: 730)"
    )
    backfill.add_argument(
        "--timeframe",
        default=Timeframe.M15.value,
        choices=[frame.value for frame in Timeframe],
        help="Candle interval (default: 15m)",
    )
    backfill.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Limit to one symbol; repeat for several. Default: every enabled pair.",
    )

    backtest = subparsers.add_parser(
        "backtest", help="Replay the baseline strategy over stored candles"
    )
    backtest.add_argument("--symbol", default="BTCUSDT")
    backtest.add_argument(
        "--timeframe",
        default=Timeframe.M15.value,
        choices=[frame.value for frame in Timeframe],
    )
    backtest.add_argument("--days", type=int, default=730)

    arguments = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(log_level=settings.log_level, environment=settings.app_env)
    register_configured_secrets(settings.secret_values())

    if arguments.command == "seed":
        return asyncio.run(_seed())
    if arguments.command == "show-config":
        return asyncio.run(_show_config())
    if arguments.command == "backtest":
        return asyncio.run(
            _backtest(arguments.symbol, Timeframe(arguments.timeframe), arguments.days)
        )
    if arguments.command == "backfill":
        if arguments.days < 1:
            parser.error("--days must be at least 1")
        return asyncio.run(
            _backfill(arguments.days, Timeframe(arguments.timeframe), arguments.symbols)
        )

    # argparse's `required=True` on the subparsers makes any other value
    # unreachable; parser.error never returns.
    parser.error(f"Unknown command: {arguments.command}")


if __name__ == "__main__":
    sys.exit(main())
