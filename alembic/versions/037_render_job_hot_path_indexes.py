"""render_job hot-path indexes

Issue #66 (C10): the render worker's claim and the scheduler's per-tick due
check read ``render_job``, which only grows (one row per render, export and
scheduled or archive job, kept as history). Two indexes serve them:

* ``ix_render_job_active`` on ``(id)``, partial on ``status IN ('pending',
  'encoding')``. It holds only the in-flight jobs, so the worker's
  oldest-pending claim, its watchdog reads and the scheduler's in-flight check
  walk a handful of entries in id order instead of the whole history.
* ``ix_render_job_project_kind_created`` on ``(project_id, kind,
  created_at)``. It covers the scheduler's newest-job-per-(project, kind)
  aggregate, so that read never touches a table row.

Index-only: no table is rebuilt and no row is touched, so no foreign key can
cascade. Both are mirrored in ``RenderJob.__table_args__`` so the model and the
schema agree.

Revision ID: 037_render_job_hot_path_indexes
Revises: 036_notification_cursor
Create Date: 2026-10-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "037_render_job_hot_path_indexes"
down_revision: str | None = "036_notification_cursor"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_render_job_active",
        "render_job",
        ["id"],
        # Frozen copy of ``db.models.render_job.ACTIVE_JOB_INDEX_WHERE``.
        sqlite_where=sa.text("status IN ('pending', 'encoding')"),
    )
    op.create_index(
        "ix_render_job_project_kind_created",
        "render_job",
        ["project_id", "kind", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_render_job_project_kind_created", table_name="render_job")
    op.drop_index("ix_render_job_active", table_name="render_job")
