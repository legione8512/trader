"""Initial data seeding.

Idempotent: running it twice changes nothing. It creates the first
configuration versions and the reference rows the application needs to exist
before it can do anything at all.

Two safety defaults are deliberate:

* Trading pairs are created **disabled**. Nothing is tradable until an operator
  enables it explicitly.
* The trading configuration is created in ``SIGNAL_ONLY`` mode regardless of
  what the environment says, so seeding a fresh database can never produce a
  system that is armed to trade.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.domain.enums import AuditActor, AutonomyMode, DailyPnlBasis, Timeframe
from app.persistence.models import (
    Exchange,
    RiskConfiguration,
    TradingConfiguration,
    TradingPair,
)
from app.persistence.repositories import (
    AuditRepository,
    ConfigurationRepository,
    ExchangeRepository,
)

BINANCE_CODE = "BINANCE"
BINANCE_NAME = "Binance Spot"

#: Phase 0 decision OD-01. Availability and filters are verified against the
#: official exchange information endpoint in Phase 3, never assumed here.
INITIAL_PAIRS: tuple[tuple[str, str, str], ...] = (
    ("BTCUSDT", "BTC", "USDT"),
    ("ETHUSDT", "ETH", "USDT"),
)


@dataclass(frozen=True, slots=True)
class SeedResult:
    """What seeding actually changed, so the caller can report it honestly."""

    exchange_created: bool
    pairs_created: tuple[str, ...]
    risk_configuration_version: int | None
    trading_configuration_version: int | None

    @property
    def changed_anything(self) -> bool:
        return (
            self.exchange_created
            or bool(self.pairs_created)
            or self.risk_configuration_version is not None
            or self.trading_configuration_version is not None
        )


async def seed(session: AsyncSession, settings: Settings) -> SeedResult:
    """Create the initial reference data and configuration if absent."""
    exchanges = ExchangeRepository(session)
    configurations = ConfigurationRepository(session)
    audit = AuditRepository(session)

    exchange = await exchanges.get_by_code(BINANCE_CODE)
    exchange_created = exchange is None
    if exchange is None:
        exchange = await exchanges.add(
            Exchange(code=BINANCE_CODE, name=BINANCE_NAME, is_enabled=True)
        )
        await audit.record(
            event_type="EXCHANGE_REGISTERED",
            actor=AuditActor.SYSTEM,
            aggregate_type="Exchange",
            aggregate_id=exchange.id,
            payload={"code": BINANCE_CODE},
            summary=f"Registered exchange {BINANCE_CODE}.",
        )

    pairs_created: list[str] = []
    for symbol, base_asset, quote_asset in INITIAL_PAIRS:
        if await exchanges.get_pair(exchange.id, symbol) is not None:
            continue
        pair = await exchanges.add_pair(
            TradingPair(
                exchange_id=exchange.id,
                symbol=symbol,
                base_asset=base_asset,
                quote_asset=quote_asset,
                # Disabled on purpose. Enabling a pair is an operator decision.
                is_enabled=False,
            )
        )
        pairs_created.append(symbol)
        await audit.record(
            event_type="TRADING_PAIR_REGISTERED",
            actor=AuditActor.SYSTEM,
            aggregate_type="TradingPair",
            aggregate_id=pair.id,
            payload={"symbol": symbol, "isEnabled": False},
            summary=f"Registered {symbol}, disabled until an operator enables it.",
        )

    risk_version: int | None = None
    if await configurations.get_active_risk_configuration() is None:
        risk_version = await configurations.next_risk_version()
        risk_configuration = RiskConfiguration(
            version=risk_version,
            reference_capital_ron=settings.bootstrap_reference_capital_ron,
            reporting_currency=settings.reporting_currency,
            session_target_percent=settings.bootstrap_session_target_percent,
            session_restart_threshold_percent=(
                settings.bootstrap_session_restart_threshold_percent
            ),
            daily_maximum_loss_percent=settings.bootstrap_daily_maximum_loss_percent,
            daily_pnl_basis=DailyPnlBasis.REALISED_PLUS_UNREALISED,
            daily_profit_giveback_percent=settings.bootstrap_daily_profit_giveback_percent,
            maximum_risk_per_trade_percent=settings.bootstrap_maximum_risk_per_trade_percent,
            maximum_open_positions=settings.bootstrap_maximum_open_positions,
            maximum_trades_per_day=settings.bootstrap_maximum_trades_per_day,
            maximum_consecutive_losses=settings.bootstrap_maximum_consecutive_losses,
            no_new_entry_minutes_before_day_end=(
                settings.bootstrap_no_new_entry_minutes_before_day_end
            ),
            created_by="SEED",
            note="Initial configuration from Phase 0 decisions. Market quality "
            "gates are intentionally NULL until calibrated on real data.",
        )
        await configurations.activate_risk_configuration(risk_configuration)
        await audit.record(
            event_type="RISK_CONFIGURATION_ACTIVATED",
            actor=AuditActor.SYSTEM,
            aggregate_type="RiskConfiguration",
            aggregate_id=risk_configuration.id,
            new_state="ACTIVE",
            payload={
                "version": risk_version,
                "referenceCapitalRon": str(settings.bootstrap_reference_capital_ron),
                "maximumTradesPerDay": settings.bootstrap_maximum_trades_per_day,
            },
            summary=f"Activated risk configuration version {risk_version}.",
        )

    trading_version: int | None = None
    if await configurations.get_active_trading_configuration() is None:
        trading_version = await configurations.next_trading_version()
        trading_configuration = TradingConfiguration(
            version=trading_version,
            # Always SIGNAL_ONLY on a fresh database, whatever the environment
            # says. Seeding must never produce a system armed to trade.
            autonomy_mode=AutonomyMode.SIGNAL_ONLY,
            emergency_stop_active=False,
            reporting_currency=settings.reporting_currency,
            exchange_quote_currency=settings.exchange_quote_currency,
            trading_timezone=settings.trading_timezone,
            primary_timeframe=Timeframe.M15,
            fx_rate_source=settings.fx_rate_source,
            usdt_usd_peg=settings.usdt_usd_peg,
            trading_windows=None,
            created_by="SEED",
            note="Initial configuration. SIGNAL_ONLY regardless of environment.",
        )
        await configurations.activate_trading_configuration(trading_configuration)
        await audit.record(
            event_type="TRADING_CONFIGURATION_ACTIVATED",
            actor=AuditActor.SYSTEM,
            aggregate_type="TradingConfiguration",
            aggregate_id=trading_configuration.id,
            new_state="ACTIVE",
            payload={"version": trading_version, "autonomyMode": AutonomyMode.SIGNAL_ONLY.value},
            summary=f"Activated trading configuration version {trading_version}.",
        )

    return SeedResult(
        exchange_created=exchange_created,
        pairs_created=tuple(pairs_created),
        risk_configuration_version=risk_version,
        trading_configuration_version=trading_version,
    )
