"""Strategies and their versions.

A signal references a **version**, never a strategy. Parameters change; a signal
generated last month must still say which exact parameter set produced it, or
the backtest that justified it cannot be reproduced.

The strategy logic itself lives in ``app/strategies`` and arrives in Phase 4.
These tables only record identity, parameters and lineage.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.persistence.base import Base
from app.persistence.mixins import CreatedAtMixin, TimestampMixin, UuidPrimaryKeyMixin


class Strategy(UuidPrimaryKeyMixin, TimestampMixin, Base):
    """A named decision procedure."""

    __tablename__ = "strategy"

    name: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    #: Disabled by default. Enabling a strategy is an operator decision, and
    #: rule R-? refuses a signal from a disabled strategy (STRATEGY_DISABLED).
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    versions: Mapped[list[StrategyVersion]] = relationship(
        back_populates="strategy",
        cascade="all, delete-orphan",
        order_by="StrategyVersion.version",
    )

    def __repr__(self) -> str:
        return f"<Strategy {self.name} enabled={self.is_enabled}>"


class StrategyVersion(UuidPrimaryKeyMixin, CreatedAtMixin, Base):
    """One immutable parameter set for a strategy.

    Append-only: changing a parameter creates a new version. There is no
    ``updated_at`` because editing a version in place would silently rewrite the
    history of every signal that referenced it.
    """

    __tablename__ = "strategy_version"
    __table_args__ = (
        Index("uq_strategy_version_number", "strategy_id", "version", unique=True),
        # At most one active version per strategy, enforced by the database.
        Index(
            "uq_strategy_version_active",
            "strategy_id",
            unique=True,
            postgresql_where=text("is_active"),
        ),
    )

    strategy_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("strategy.id", ondelete="CASCADE"), nullable=False, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    #: The exact parameter set. Stored as JSON because the shape belongs to the
    #: strategy, not to this table, and a new strategy must not require a
    #: migration.
    parameters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    #: Hash of the decision code that produced this version. Two runs with the
    #: same parameters but different code are NOT the same experiment, and
    #: reproducible backtesting (AC-20) depends on being able to tell them apart.
    code_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="SYSTEM")

    strategy: Mapped[Strategy] = relationship(back_populates="versions")

    def __repr__(self) -> str:
        return f"<StrategyVersion v{self.version} active={self.is_active}>"
