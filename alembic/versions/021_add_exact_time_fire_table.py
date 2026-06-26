"""add exact-time fire-log table

Creates the durable fire-log for exact-time capture: one decision row per anchor
per local day, guaranteeing each anchor fires exactly once a day across restarts
and clock jitter.

* New ``exact_time_fire`` table:
  - ``project_id`` -- FK to ``project.id`` with ``ON DELETE CASCADE`` (fire rows
    vanish with their project).
  - ``anchor_id`` -- the anchor's stable generated id.
  - ``local_date`` -- the ``YYYY-MM-DD`` local fire day (schedule timezone).
  - ``status`` -- ``captured`` / ``failed`` / ``skipped_missed`` /
    ``skipped_no_geo``.
  - ``fired_at`` -- when the decision row was written (naive UTC).
  - ``frame_id`` -- FK to ``frame.id`` (``SET NULL``), the written frame when
    captured, else ``NULL``.
  - ``detail`` -- short human-readable reason for a skip/fail, else ``NULL``.
  - Unique constraint ``uq_exact_time_fire`` on
    ``(project_id, anchor_id, local_date)`` -- the once-per-day idempotency
    guard; a duplicate insert is the double-fire backstop.

Revision ID: 021_add_exact_time_fire_table
Revises: 020_add_project_exact_time_anchors
Create Date: 2026-06-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "021_add_exact_time_fire_table"
down_revision: str | None = "020_add_project_exact_time_anchors"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "exact_time_fire",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("anchor_id", sa.String(), nullable=False),
        sa.Column("local_date", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("fired_at", sa.DateTime(), nullable=False),
        sa.Column("frame_id", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["project.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["frame_id"],
            ["frame.id"],
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "anchor_id",
            "local_date",
            name="uq_exact_time_fire",
        ),
    )


def downgrade() -> None:
    op.drop_table("exact_time_fire")
