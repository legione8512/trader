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

from app.config.settings import get_settings
from app.core.logging import configure_logging, get_logger, register_configured_secrets
from app.domain.enums import Timeframe
from app.exchanges.binance.market_data import BinanceMarketDataAdapter
from app.exchanges.binance.rest import BinanceRestClient
from app.market_data.history import HistoryDownloader
from app.persistence.candles import CandleRepository
from app.persistence.database import build_engine, build_session_factory
from app.persistence.repositories import ConfigurationRepository, ExchangeRepository
from app.persistence.seed import BINANCE_CODE, seed

logger = get_logger(__name__)


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

    arguments = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(log_level=settings.log_level, environment=settings.app_env)
    register_configured_secrets(settings.secret_values())

    if arguments.command == "seed":
        return asyncio.run(_seed())
    if arguments.command == "show-config":
        return asyncio.run(_show_config())
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
