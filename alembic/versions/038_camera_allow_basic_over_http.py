"""camera Basic-over-HTTP opt-in

Issue #69 (CAM-S1, owner decision OD-3): HTTP Basic authentication sends the
camera password merely base64-encoded, so over plain ``http://`` anyone on the
path can read it. The capture path now refuses a Basic-only offer over plain
HTTP unless the camera opts in. This adds the per-camera flag:

* ``camera.allow_basic_over_http`` BOOLEAN NOT NULL, server default ``0``.

Every existing row reads ``0`` (off). A camera reached over ``https://``, or one
that offers Digest, is unaffected; only a plain-HTTP camera that offers nothing
but Basic stops until the operator ticks the box.

Native ``ALTER TABLE ... ADD COLUMN``: the ``camera`` table is not rebuilt, so
no ``ON DELETE CASCADE`` from ``project`` can fire. The downgrade is a native
``DROP COLUMN`` (SQLite 3.35 or later; the frozen build bundles 3.53.1).

Revision ID: 038_camera_allow_basic_over_http
Revises: 037_render_job_hot_path_indexes
Create Date: 2026-10-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "038_camera_allow_basic_over_http"
down_revision: str | None = "037_render_job_hot_path_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "camera",
        sa.Column(
            "allow_basic_over_http",
            sa.Boolean(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("camera", "allow_basic_over_http")
