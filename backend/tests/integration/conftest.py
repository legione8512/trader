"""Integration test fixtures.

These tests run against a real PostgreSQL. SQLite would be faster and needs no
container, but it would also be a lie: SQLite has no NUMERIC semantics, no
partial unique indexes and no JSONB. The properties this milestone is about are
exactly the ones SQLite cannot check.

The schema is built by running the real Alembic migrations, not by
``create_all``. That way every test run also verifies the migrations.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config.settings import Settings

BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: Suffix appended to the application database name to obtain the test one.
TEST_DATABASE_SUFFIX = "_test"


def with_database_name(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


@lru_cache(maxsize=1)
def resolve_test_database_url() -> str:
    """Where the integration tests run.

    Precedence:

    1. ``TEST_DATABASE_URL`` - used by CI, which has its own PostgreSQL service
       and no ``.env`` file.
    2. Derived from the application's own ``DATABASE_URL``, with ``_test``
       appended to the database name.

    Deriving rather than hard-coding matters: the credentials in ``.env`` are
    chosen per machine, and a hard-coded default silently stops matching them.
    """
    override = os.environ.get("TEST_DATABASE_URL")
    if override:
        url = override
    else:
        application_url = Settings().database_url
        application_name = urlsplit(application_url).path.lstrip("/")
        url = with_database_name(application_url, f"{application_name}{TEST_DATABASE_SUFFIX}")

    name = urlsplit(url).path.lstrip("/")
    if not name.endswith(TEST_DATABASE_SUFFIX):
        # Integration tests run migrations against this database. Pointing them
        # at the development or production database would rewrite its schema.
        raise RuntimeError(
            f"Refusing to run integration tests against database {name!r}: "
            f"the name must end with {TEST_DATABASE_SUFFIX!r}."
        )
    return url


def maintenance_url(url: str) -> str:
    """The same server, but the always-present ``postgres`` database.

    Creating a database cannot be done from inside that database.
    """
    return with_database_name(url, "postgres")


async def ensure_database_exists(url: str) -> None:
    name = urlsplit(url).path.lstrip("/")
    engine = create_async_engine(maintenance_url(url), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            result = await connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            )
            if result.scalar_one_or_none() is None:
                # An identifier cannot be parameterised. The name comes from our
                # own configuration, never from user input.
                await connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()


def run_migrations(url: str) -> None:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))

    previous = os.environ.get("ALEMBIC_DATABASE_URL")
    os.environ["ALEMBIC_DATABASE_URL"] = url
    try:
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("ALEMBIC_DATABASE_URL", None)
        else:
            os.environ["ALEMBIC_DATABASE_URL"] = previous


@pytest.fixture(scope="session")
def database_url() -> str:
    return resolve_test_database_url()


@pytest.fixture(scope="session", autouse=True)
def prepared_database(database_url: str) -> None:
    """Create the test database once per session and migrate it to head.

    Synchronous on purpose: it owns its own event loop, which keeps it free of
    the loop-scope rules that apply to async fixtures.
    """
    asyncio.run(ensure_database_exists(database_url))
    run_migrations(database_url)


@pytest.fixture
async def db_connection(database_url: str) -> AsyncIterator[AsyncConnection]:
    """A connection inside a transaction that is always rolled back.

    Every test starts from the same migrated, empty schema. Nothing a test
    writes survives it, so tests stay independent and their order never matters.
    """
    engine = create_async_engine(database_url)
    connection = await engine.connect()
    transaction = await connection.begin()
    try:
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


@pytest.fixture
async def db_session(db_connection: AsyncConnection) -> AsyncIterator[AsyncSession]:
    """A session bound to the rolled-back transaction.

    ``join_transaction_mode="create_savepoint"`` means a ``session.commit()``
    inside the code under test commits a SAVEPOINT, not the outer transaction.
    Production code can therefore commit normally while the test still discards
    everything at the end.
    """
    factory = async_sessionmaker(
        bind=db_connection,
        expire_on_commit=False,
        autoflush=False,
        join_transaction_mode="create_savepoint",
    )
    async with factory() as session:
        yield session
