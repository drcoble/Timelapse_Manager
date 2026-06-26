"""add project PTZ positioning columns

Lets a project drive its camera to a fixed pan/tilt/zoom position before each
capture, so a single PTZ camera can serve several projects pointed in different
directions:

* ``project.ptz_preset`` (nullable string) -- the name/id of a camera-defined
  PTZ preset to recall.
* ``project.ptz_pan`` / ``project.ptz_tilt`` / ``project.ptz_zoom`` (nullable
  floats) -- a raw position in the camera's own units, forwarded verbatim.

All four are purely additive and nullable; ``NULL`` means "leave the camera
where it is", which is the behaviour for every existing project (no backfill, no
default). The numeric values are not range-constrained here.

Revision ID: 013_add_project_ptz
Revises: 012_add_event_alert_lifecycle
Create Date: 2026-06-18

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "013_add_project_ptz"
down_revision: str | None = "012_add_event_alert_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.add_column(sa.Column("ptz_preset", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("ptz_pan", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("ptz_tilt", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("ptz_zoom", sa.Float(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_column("ptz_zoom")
        batch_op.drop_column("ptz_tilt")
        batch_op.drop_column("ptz_pan")
        batch_op.drop_column("ptz_preset")
