"""add project exact-time anchors

Introduces exact-time capture: a project may carry a list of anchors describing
instants (a wall-clock time, or solar noon for the camera's location) at which a
single frame is captured once per local day, independent of the recurring
capture-gating schedule.

* New ``project.exact_time_anchors`` column (nullable JSON, a list of anchor
  objects). Stored in a dedicated column -- not folded into ``schedule`` -- so
  the schedule form's rebuild and preset detection never disturb it.

The column is additive and nullable, so every pre-existing row reads back as
``NULL`` (no anchors), preserving today's behaviour.

Revision ID: 020_add_project_exact_time_anchors
Revises: 019_add_frame_capture_reason
Create Date: 2026-06-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "020_add_project_exact_time_anchors"
down_revision: str | None = "019_add_frame_capture_reason"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.add_column(sa.Column("exact_time_anchors", sa.JSON(), nullable=True))


def downgrade() -> None:
    # Dropping a column makes SQLite rebuild ``project`` (create-copy-drop-rename).
    # The implicit DROP of the old table cascades through the ON DELETE CASCADE
    # foreign keys on ``frame``/``render_job``/``milestone`` and would silently
    # delete those child rows. Suspend FK enforcement across the rebuild so child
    # data survives; this works because Alembic applies SQLite DDL outside a
    # transaction here (PRAGMA foreign_keys is a no-op mid-transaction).
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    if is_sqlite:
        op.execute("PRAGMA foreign_keys=OFF")
    try:
        with op.batch_alter_table("project") as batch_op:
            batch_op.drop_column("exact_time_anchors")
    finally:
        if is_sqlite:
            op.execute("PRAGMA foreign_keys=ON")
