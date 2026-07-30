"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine

from app import __version__
from app.api.routes import health as health_routes
from app.config.settings import Settings, get_settings
from app.core.logging import configure_logging, get_logger, register_configured_secrets
from app.domain.enums import AutonomyMode
from app.market_data.freshness import FeedFreshnessMonitor
from app.monitoring.health import build_default_registry
from app.persistence.database import build_engine, build_session_factory

logger = get_logger(__name__)

API_PREFIX = "/api"


def create_app(settings: Settings | None = None, *, engine: AsyncEngine | None = None) -> FastAPI:
    """Build the application.

    A factory rather than a module-level instance, so tests can build isolated
    applications with different configurations.

    The engine can be injected; otherwise one is built from settings. Building
    an engine opens no connection, so this never blocks startup.
    """
    active_settings = settings if settings is not None else get_settings()

    configure_logging(
        log_level=active_settings.log_level,
        environment=active_settings.app_env,
    )
    # Registered before the first log line that could contain a credential.
    registered = register_configured_secrets(active_settings.secret_values())

    active_engine = engine if engine is not None else build_engine(active_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        logger.info(
            "application_starting",
            version=__version__,
            environment=active_settings.app_env.value,
            autonomy_mode=active_settings.autonomy_mode.value,
            live_trading_enabled=active_settings.live_trading_enabled,
            reporting_currency=active_settings.reporting_currency,
            exchange_quote_currency=active_settings.exchange_quote_currency,
            trading_timezone=active_settings.trading_timezone,
            reference_capital_ron=str(active_settings.bootstrap_reference_capital_ron),
            # Named without the word "secret": the masker redacts by key name,
            # and would otherwise hide this harmless count.
            masked_value_count=registered,
        )
        if active_settings.autonomy_mode is AutonomyMode.LIVE_AUTOMATIC:
            logger.warning(
                "live_trading_mode_active",
                detail="Real orders may be submitted once the runtime operator "
                "confirmation is granted.",
            )
        try:
            yield
        finally:
            await active_engine.dispose()
            logger.info("application_stopping", version=__version__)

    app = FastAPI(
        title="Trader",
        description=(
            "Automated cryptocurrency spot trading application. "
            "Refuses to trade when no sufficiently strong opportunity exists."
        ),
        version=__version__,
        lifespan=lifespan,
    )

    app.state.settings = active_settings
    app.state.engine = active_engine
    app.state.session_factory = build_session_factory(active_engine)
    # Shared by the ingestion service, which records candle arrivals, and by the
    # health endpoint, which reads them. Created here so both see one instance.
    #
    # The market-data check is NOT registered yet: it is registered by whatever
    # starts a feed, together with the feeds it expects. Registering it while
    # nothing ingests would report "no market data" as a permanent condition of
    # the deployment rather than as the state of a feed.
    app.state.feed_freshness = FeedFreshnessMonitor()
    app.state.health_registry = build_default_registry(active_settings, active_engine)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=active_settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["*"],
    )

    api_router = APIRouter(prefix=API_PREFIX)
    api_router.include_router(health_routes.router)
    app.include_router(api_router)

    return app


# NOTE: there is deliberately no module-level ``app = create_app()``.
#
# Creating the application at import time would load and validate configuration
# as a side effect of importing this module, so any test, script or tool that
# merely imports ``app.main`` would need a complete valid environment.
#
# The application is started through the factory instead:
#
#     uvicorn app.main:create_app --factory
