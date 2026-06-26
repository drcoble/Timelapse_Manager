"""add project campaign bounds

Adds first-class campaign-bound columns to ``project``:

* ``start_date`` -- capture does not run before this instant;
* ``end_date`` -- capture stops once ``now`` reaches this instant;
* ``max_frame_count`` -- capture stops once the active frame count reaches this.

All three are nullable; a project with none set runs open-endedly, preserving
the prior behaviour. The datetimes are stored as naive UTC, matching the other
``DateTime`` columns. These are distinct from the recurring daily ``schedule``
window: they bound the campaign as a whole rather than gating a time of day.

Revision ID: 005_add_project_campaign_bounds
Revises: 004_add_paused_lifecycle_state
Create Date: 2026-06-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "005_add_project_campaign_bounds"
down_revision: str | None = "004_add_paused_lifecycle_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.add_column(sa.Column("start_date", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("end_date", sa.DateTime(), nullable=True))
        batch_op.add_column(
            sa.Column("max_frame_count", sa.Integer(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_column("max_frame_count")
        batch_op.drop_column("end_date")
        batch_op.drop_column("start_date")
