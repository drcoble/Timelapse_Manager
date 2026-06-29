"""add project stream identity

Lets a project pick which of its camera's named streams/profiles it captures
from, rather than always using the camera's default stream:

* New ``project.stream_id`` column (nullable) -- the identifier of the selected
  stream. ``NULL`` means "use the camera default", which is the behaviour for
  every existing project (no backfill, no default).
* New ``project.stream_label`` column (nullable) -- the human-readable name of
  the selected stream, stored alongside the id so the UI can display it without
  re-querying the camera.

Both columns are purely additive and nullable; no data is migrated.

Revision ID: 010_add_project_stream_identity
Revises: 009_add_camera_default_credentials
Create Date: 2026-06-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "010_add_project_stream_identity"
down_revision: str | None = "009_add_camera_default_credentials"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.add_column(sa.Column("stream_id", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("stream_label", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_column("stream_label")
        batch_op.drop_column("stream_id")
