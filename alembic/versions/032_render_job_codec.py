"""add render_job codec string and MIME type

Issue #25: the server no longer decides which renders play in a browser; each
viewer's browser does, from the render's MIME type and RFC 6381 codec string.
The render worker probes every finished render with ffprobe and stores both:

* New ``render_job.codec_string`` (nullable String) -- e.g. ``avc1.64001F``,
  ``hvc1.1.6.L93.B0``, ``vp09.00.10.08``, ``av01.0.05M.08``.
* New ``render_job.mime_type`` (nullable String) -- ``video/mp4``,
  ``video/webm`` or ``video/x-matroska``.

The columns are additive and nullable. Existing renders are **not** probed
here (owner decision D14): a migration must not shell out to ffprobe over every
file. The render worker backfills them once after startup, one at a time; until
then (or if a probe fails) the page falls back to a MIME-only check.

Revision ID: 032_render_job_codec
Revises: 031_entity_autoincrement
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from timelapse_manager.db.migration_utils import suspend_foreign_keys

# revision identifiers, used by Alembic.
revision: str = "032_render_job_codec"
down_revision: str | None = "031_entity_autoincrement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("render_job") as batch_op:
        batch_op.add_column(sa.Column("codec_string", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("mime_type", sa.String(), nullable=True))


def downgrade() -> None:
    # Dropping a column makes SQLite rebuild ``render_job``. Suspend FK
    # enforcement across the rebuild so no ON DELETE CASCADE can fire on the
    # implicit DROP of the old table, and keep the table's AUTOINCREMENT
    # (migration 028) on the rebuilt copy.
    with (
        suspend_foreign_keys(op),
        op.batch_alter_table(
            "render_job", table_kwargs={"sqlite_autoincrement": True}
        ) as batch_op,
    ):
        batch_op.drop_column("mime_type")
        batch_op.drop_column("codec_string")
