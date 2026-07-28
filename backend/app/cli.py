"""Command line entry points.

Usage:

    python -m app.cli seed
    python -m app.cli show-config
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.config.settings import get_settings
from app.core.logging import configure_logging, get_logger, register_configured_secrets
from app.persistence.database import build_engine, build_session_factory
from app.persistence.repositories import ConfigurationRepository
from app.persistence.seed import seed

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="app.cli", description="Trader maintenance commands")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("seed", help="Create initial reference data and configuration")
    subparsers.add_parser("show-config", help="Print the active configuration")

    arguments = parser.parse_args(argv)

    settings = get_settings()
    configure_logging(log_level=settings.log_level, environment=settings.app_env)
    register_configured_secrets(settings.secret_values())

    if arguments.command == "seed":
        return asyncio.run(_seed())
    if arguments.command == "show-config":
        return asyncio.run(_show_config())

    # argparse's `required=True` on the subparsers makes any other value
    # unreachable; parser.error never returns.
    parser.error(f"Unknown command: {arguments.command}")


if __name__ == "__main__":
    sys.exit(main())
