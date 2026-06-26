"""add render_job export kind

Widens the render-job ``kind`` enum from ``(manual, scheduled, archive)`` to
``(manual, scheduled, archive, export)`` so the bounded render worker can also
drain *export* jobs -- a job that zips a selection of frame image files instead
of encoding a video. An export job reuses the render-job row's queue, status,
and output-path machinery; only its ``kind`` distinguishes it.

The enum is portable (``native_enum=False``), so on SQLite it is a ``VARCHAR``
column rather than a real database enum type. On this database that ``VARCHAR``
carries **no** named ``CHECK`` constraint (unlike a native enum), so there is no
constraint to widen: this migration is a **type-metadata sync**. It still ships
and still rewrites the table via :func:`op.batch_alter_table` (alembic's
table-rebuild mode) to keep the column's declared type in lock-step with the
model, mirroring the earlier lifecycle-enum widening; the rebuild preserves the
row count and every existing value. The downgrade folds any ``export`` rows back
to ``manual`` before narrowing the declared type, so a tighter type can never
reject existing data.

Revision ID: 017_add_render_job_export_kind
Revises: 016_add_ssrf_settings
Create Date: 2026-06-22

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "017_add_render_job_export_kind"
down_revision: str | None = "016_add_ssrf_settings"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "render_job_kind"


def _enum(*values: str) -> sa.Enum:
    """Build the portable (no-native) render-job ``kind`` enum for ``values``."""
    return sa.Enum(*values, name=_NAME, native_enum=False)


def upgrade() -> None:
    with op.batch_alter_table("render_job") as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=_enum("manual", "scheduled", "archive"),
            type_=_enum("manual", "scheduled", "archive", "export"),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Fold any export rows back to manual before narrowing the declared type, so
    # the table-copy's narrower type metadata cannot reject existing data.
    op.execute("UPDATE render_job SET kind = 'manual' WHERE kind = 'export'")
    with op.batch_alter_table("render_job") as batch_op:
        batch_op.alter_column(
            "kind",
            existing_type=_enum("manual", "scheduled", "archive", "export"),
            type_=_enum("manual", "scheduled", "archive"),
            existing_nullable=False,
        )
