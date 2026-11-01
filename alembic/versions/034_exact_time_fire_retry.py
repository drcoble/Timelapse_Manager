"""exact_time_fire retry state

Issue #69 (CAM-R1): a failed exact-time anchor is retried inside its grace
window. The fire-log row carries the retry state:

* ``attempts`` (INTEGER NOT NULL, server default ``1``): how many capture
  attempts the anchor has made for that day. Existing rows read as one attempt.
* ``next_attempt_at`` (DATETIME NULL): when the next attempt is due while the
  row is ``retry_pending``.

``status`` is a plain string column (``native_enum=False`` and no CHECK
constraint, see migration 021), so the new ``retry_pending`` value needs no DDL.

Native ``ALTER TABLE ... ADD COLUMN`` only. ``exact_time_fire`` references
``project`` and ``frame``, and a table rebuild is never safe next to
``ON DELETE CASCADE`` tables, so ``batch_alter_table`` is not used. The
downgrade first rewrites ``retry_pending`` to ``failed`` (the older code does
not know the value), then drops both columns natively (SQLite 3.35 or later).

Revision ID: 034_exact_time_fire_retry
Revises: 033_event_hot_path_indexes
Create Date: 2026-10-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "034_exact_time_fire_retry"
down_revision: str | None = "033_event_hot_path_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "exact_time_fire",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
    )
    op.add_column(
        "exact_time_fire",
        sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.execute(
        "UPDATE exact_time_fire SET status = 'failed' "
        "WHERE status = 'retry_pending'"
    )
    op.drop_column("exact_time_fire", "next_attempt_at")
    op.drop_column("exact_time_fire", "attempts")
