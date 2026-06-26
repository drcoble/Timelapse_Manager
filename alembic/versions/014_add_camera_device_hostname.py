"""add camera device hostname

Records a camera's network hostname alongside the existing geolocation pair:

* New ``camera.device_hostname`` column (nullable) -- the most recent hostname
  known for the device. ``NULL`` when the hostname has never been resolved or
  set.
* New ``camera.device_hostname_source`` column (nullable) -- whether the stored
  hostname was reported by the device (``camera``) or set by an operator
  (``manual``), mirroring ``geolocation_source`` exactly in style. The enum is
  portable (``native_enum=False``), so on SQLite it is a plain ``VARCHAR`` rather
  than a native database enum type.

Both columns are purely additive and nullable; no data is migrated.

Revision ID: 014_add_camera_device_hostname
Revises: 013_add_project_ptz
Create Date: 2026-06-18

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "014_add_camera_device_hostname"
down_revision: str | None = "013_add_project_ptz"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _source_enum() -> sa.Enum:
    """Build the portable (CHECK-constraint) hostname-source enum.

    Mirrors ``geolocation_source`` in shape: a ``native_enum=False`` enum whose
    allowed values are ``camera`` and ``manual``.
    """
    return sa.Enum(
        "camera", "manual", name="camera_device_hostname_source", native_enum=False
    )


def upgrade() -> None:
    with op.batch_alter_table("camera") as batch_op:
        batch_op.add_column(sa.Column("device_hostname", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("device_hostname_source", _source_enum(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("camera") as batch_op:
        batch_op.drop_column("device_hostname_source")
        batch_op.drop_column("device_hostname")
