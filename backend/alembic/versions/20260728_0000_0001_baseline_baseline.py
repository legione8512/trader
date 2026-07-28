"""baseline

Establishes the migration chain. Creates no tables: the domain schema arrives
in Phase 2.

Applying it creates Alembic's own ``alembic_version`` table, which is what
makes every later migration ordered, repeatable and reversible. Starting the
chain from an empty, explicit revision means there is never an ambiguous
"before migrations existed" state.

Revision ID: 0001_baseline
Revises:
Create Date: 2026-07-28
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "0001_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
