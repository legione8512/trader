"""SQLAlchemy declarative base.

No domain models live here yet - those arrive in Phase 2. What this module
establishes now is the *naming convention*, and it has to exist before the very
first migration.
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

#: Deterministic names for every constraint and index.
#:
#: Without this, PostgreSQL invents names such as ``trading_day_date_key``, and
#: Alembic generates migrations that reference those invented names. The same
#: model then produces different constraint names on different databases, and a
#: downgrade written against one database fails against another.
#:
#: With an explicit convention, a constraint name is a pure function of the
#: model, so migrations stay reproducible - which is also what makes AC-20
#: (reproducible backtests, reproducible schema) achievable.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Declarative base for every persisted model."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
