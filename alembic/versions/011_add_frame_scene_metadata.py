"""add frame stream identity and scene metadata

Records, per captured frame, which named camera stream produced it and a
best-effort snapshot of the camera's scene/image settings at capture time:

* New ``frame.stream_id`` column (nullable) -- a denormalised snapshot of the
  stream identifier in force when the frame was captured. ``NULL`` for an
  uploaded frame or a frame taken from the camera's default stream. Fixing it on
  the frame keeps the provenance stable if the project later selects a different
  stream.
* New ``frame.scene_metadata`` column (nullable JSON) -- a small versioned
  envelope of scene/image settings (brightness, contrast, exposure, ...) the
  camera exposed at capture time. ``NULL`` when no metadata was collected
  (uploaded frames, protocols with no scene data, or a failed best-effort read).

Both columns are purely additive and nullable; no data is migrated.

Revision ID: 011_add_frame_scene_metadata
Revises: 010_add_project_stream_identity
Create Date: 2026-06-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "011_add_frame_scene_metadata"
down_revision: str | None = "010_add_project_stream_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("frame") as batch_op:
        batch_op.add_column(sa.Column("stream_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("scene_metadata", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("frame") as batch_op:
        batch_op.drop_column("scene_metadata")
        batch_op.drop_column("stream_id")
