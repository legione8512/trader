"""Alembic environment, async flavour.

The database URL is read from validated ``Settings`` rather than from
``alembic.ini``, so the password exists in exactly one place and never in a
committed file.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from typing import Any

from alembic import context
from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

from app.config.settings import get_settings
from app.persistence import models as _models  # noqa: F401  (registers every model)
from app.persistence.base import Base
from app.persistence.types import UtcDateTime

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate compares the live database against this metadata. Importing
# app.persistence.models above registers every table; a model that is not
# imported would be invisible here, and autogenerate would silently propose
# dropping its table.
target_metadata = Base.metadata


def get_database_url() -> str:
    """Resolve the target database, in order of precedence.

    ``-x db_url=...`` beats ``ALEMBIC_DATABASE_URL`` beats validated Settings.
    The overrides exist so integration tests can migrate a throwaway database
    without mutating process-wide configuration.
    """
    x_arguments = context.get_x_argument(as_dictionary=True)
    if "db_url" in x_arguments:
        return str(x_arguments["db_url"])

    override = os.environ.get("ALEMBIC_DATABASE_URL")
    if override:
        return override

    return get_settings().database_url


def render_item(type_: str, obj: Any, autogen_context: AutogenContext) -> str | bool:
    """Render custom types using their plain SQLAlchemy equivalent.

    A migration must be self-contained. If it referenced
    ``app.persistence.types.UtcDateTime`` and that module were later renamed or
    removed, every historical migration would stop running - and the ability to
    rebuild the database from scratch is exactly what makes an audit trail
    trustworthy.

    ``UtcDateTime`` is a ``TypeDecorator`` over ``DateTime(timezone=True)``: it
    only affects Python-side binding, so the emitted DDL is identical.
    """
    if type_ == "type" and isinstance(obj, UtcDateTime):
        # No import is registered here: script.py.mako already imports
        # sqlalchemy as sa, and adding it again produces a duplicate import in
        # every generated migration.
        return "sa.DateTime(timezone=True)"
    return False


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting, for review or manual execution."""
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_item=render_item,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_item=render_item,
        # Detect column type and server-default changes, not just added and
        # dropped columns. A NUMERIC(18, 8) silently becoming NUMERIC(10, 2)
        # would round money away.
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    engine = create_async_engine(get_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
