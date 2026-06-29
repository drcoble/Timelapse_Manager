"""add frame capture-timestamp inferred flag

Records, per frame, whether its ``capture_timestamp`` was *inferred* rather than
read from the image:

* New ``frame.capture_timestamp_inferred`` column (non-null BOOLEAN, default
  ``false``) -- ``False`` means the timestamp came from a live capture or a
  readable Exif ``DateTimeOriginal``; ``True`` means an import fell back to a
  caller-supplied time because the bytes carried no readable Exif capture time.
  The browser badges inferred frames and offers an inline correction; the flag
  is cleared once the user edits the timestamp.

The column is additive. ``server_default=false()`` backfills ``0`` into every
pre-existing row, so existing captured frames correctly read back ``False``.

Revision ID: 018_add_frame_capture_timestamp_inferred
Revises: 017_add_render_job_export_kind
Create Date: 2026-06-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "018_add_frame_capture_timestamp_inferred"
down_revision: str | None = "017_add_render_job_export_kind"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("frame") as batch_op:
        batch_op.add_column(
            sa.Column(
                "capture_timestamp_inferred",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("frame") as batch_op:
        batch_op.drop_column("capture_timestamp_inferred")
