"""add frame render-exclusion timestamp

Records, per frame, whether it is excluded from rendered output:

* New ``frame.excluded_at`` column (nullable DATETIME) -- ``NULL`` means the
  frame is included in renders (the state of every existing frame); a non-null
  value is the instant the frame was excluded. Excluded frames stay fully
  visible in the browser and are skipped only by the encoder, so this flag is
  orthogonal to ``lifecycle_state`` -- a frame can be both soft-deleted and
  excluded.

The column is purely additive and nullable; no data is migrated (every existing
frame reads back ``NULL`` = included, which is correct).

Revision ID: 015_add_frame_excluded_at
Revises: 014_add_camera_device_hostname
Create Date: 2026-06-21

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "015_add_frame_excluded_at"
down_revision: str | None = "014_add_camera_device_hostname"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("frame") as batch_op:
        batch_op.add_column(sa.Column("excluded_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("frame") as batch_op:
        batch_op.drop_column("excluded_at")
