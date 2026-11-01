"""add render_job frame window

Issue #9: the render card shows which frames went into a video (or a frame
export's zip), but nothing recorded that once the job ran. The render worker now
stores it on the job as it encodes:

* New ``render_job.frame_count`` (nullable Integer) -- frames encoded into the
  video, or bundled into an export's zip.
* New ``render_job.first_frame_at`` / ``render_job.last_frame_at`` (nullable
  DateTime, naive UTC) -- capture times of the first and last of those frames.

The columns are additive and nullable: every pre-existing job reads back as
``NULL``, which the card shows as "not recorded".

Revision ID: 026_add_render_job_frame_window
Revises: 025_add_user_username_key
Create Date: 2026-10-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from timelapse_manager.db.migration_utils import suspend_foreign_keys

# revision identifiers, used by Alembic.
revision: str = "026_add_render_job_frame_window"
down_revision: str | None = "025_add_user_username_key"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("render_job") as batch_op:
        batch_op.add_column(sa.Column("frame_count", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("first_frame_at", sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column("last_frame_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    # Dropping a column makes SQLite rebuild ``render_job``. Suspend FK
    # enforcement across the rebuild so no ON DELETE CASCADE can fire on the
    # implicit DROP of the old table.
    with suspend_foreign_keys(op), op.batch_alter_table("render_job") as batch_op:
        batch_op.drop_column("last_frame_at")
        batch_op.drop_column("first_frame_at")
        batch_op.drop_column("frame_count")
