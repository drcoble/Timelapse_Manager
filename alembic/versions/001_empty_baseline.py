"""empty baseline

Establishes the initial migration head without creating any tables. Real
schema is introduced by later migrations that build on this baseline.

Revision ID: 001_empty_baseline
Revises:
Create Date: 2026-06-09

"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "001_empty_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No-op: the baseline intentionally creates no tables."""


def downgrade() -> None:
    """No-op: nothing was created by the baseline."""
