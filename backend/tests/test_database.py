"""Database wiring tests.

None of these require a running PostgreSQL. Building an engine opens no
connection, which is exactly why the application can start and *report* a
broken database instead of refusing to boot.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import Numeric
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

from app.config.settings import Settings
from app.domain.enums import HealthStatus
from app.monitoring.health import build_database_check
from app.persistence import models as _models  # noqa: F401  (registers every table)
from app.persistence.base import NAMING_CONVENTION, Base
from app.persistence.database import build_engine, build_session_factory, ping


class TestEngine:
    def test_building_an_engine_opens_no_connection(self, settings: Settings) -> None:
        engine = build_engine(settings)
        assert isinstance(engine, AsyncEngine)
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.checkedout() == 0

    def test_engine_uses_the_configured_pool_settings(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(database_pool_size=7, database_max_overflow=3)
        engine = build_engine(settings)
        assert isinstance(engine.pool, QueuePool)
        assert engine.pool.size() == 7

    def test_session_factory_does_not_expire_on_commit(self, settings: Settings) -> None:
        """A trading decision reads, commits, then still needs the values."""
        factory = build_session_factory(build_engine(settings))
        assert factory.kw["expire_on_commit"] is False


class TestDatabaseHealthCheck:
    async def test_unreachable_database_reports_unhealthy(self, settings: Settings) -> None:
        check = build_database_check(build_engine(settings))
        result = await check()
        assert result.status is HealthStatus.UNHEALTHY
        assert result.name == "database"

    async def test_failure_detail_leaks_no_credentials(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(
            database_url="postgresql+asyncpg://trader:leaky-password-value@127.0.0.1:1/trader"
        )
        check = build_database_check(build_engine(settings))
        result = await check()
        assert result.detail is not None
        assert "leaky-password-value" not in result.detail
        assert "trader" not in result.detail

    async def test_ping_raises_when_the_database_is_unreachable(self, settings: Settings) -> None:
        """A refused connection is NOT always a SQLAlchemyError.

        On Windows, asyncpg lets the raw ``ConnectionRefusedError`` (an
        ``OSError``) propagate before SQLAlchemy can wrap it. This is precisely
        why the health check catches broad ``Exception`` rather than
        ``SQLAlchemyError``: narrowing it would let a real outage crash the
        health endpoint instead of being reported by it.
        """
        with pytest.raises((SQLAlchemyError, OSError)):
            await ping(build_engine(settings))


class TestNamingConvention:
    def test_metadata_uses_the_explicit_convention(self) -> None:
        """Constraint names must be a pure function of the model."""
        assert Base.metadata.naming_convention == NAMING_CONVENTION

    def test_the_expected_tables_are_registered(self) -> None:
        """Importing app.persistence.models must register every table.

        A model that is not imported is invisible to Alembic autogenerate, which
        would then silently propose dropping its table.
        """
        assert set(Base.metadata.tables) == {
            "audit_event",
            "exchange",
            "fx_rate_snapshot",
            "risk_assessment",
            "risk_configuration",
            "signal",
            "strategy",
            "strategy_version",
            "system_event",
            "trading_configuration",
            "trading_day",
            "trading_pair",
            "trading_session",
        }

    def test_every_monetary_column_is_numeric(self) -> None:
        """AC-17 at the schema level: no float anywhere on the money path."""
        monetary_suffixes = ("_ron", "_percent", "_quote", "peg", "rate", "ratio")
        for table in Base.metadata.tables.values():
            for column in table.columns:
                if not column.name.endswith(monetary_suffixes):
                    continue
                if not isinstance(column.type, Numeric):
                    continue
                assert column.type.asdecimal is True, f"{table.name}.{column.name}"
                assert column.type.scale is not None, f"{table.name}.{column.name} has no scale"


class TestDatabasePasswordIsTreatedAsASecret:
    def test_password_is_extracted_from_the_url(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(
            database_url="postgresql+asyncpg://trader:p%40ssw0rd-long@localhost:5432/trader"
        )
        assert settings.database_password == "p@ssw0rd-long"

    def test_password_is_registered_for_masking(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        settings = make_settings(
            database_url="postgresql+asyncpg://trader:realdbpassword@localhost:5432/trader"
        )
        assert "realdbpassword" in settings.secret_values()

    def test_placeholder_password_is_not_registered(
        self, make_settings: Callable[..., Settings]
    ) -> None:
        """Registering "CHANGE_ME" would redact it from every log message."""
        settings = make_settings(
            database_url="postgresql+asyncpg://trader:CHANGE_ME@localhost:5432/trader"
        )
        assert settings.secret_values() == []
