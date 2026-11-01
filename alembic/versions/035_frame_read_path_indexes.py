"""frame read-path indexes

Issue #66 (C5, C6): the dashboard's per-project frame aggregates and the render
gather read the ``frame`` table, which only grows. Two plain indexes serve them:

* ``ix_frame_project_ts`` on ``(project_id, capture_timestamp,
  sequence_index)``. It matches the render gather's ORDER BY (capture time,
  then sequence index as the tie-break) exactly, so the gather streams rows in
  index order with no temp B-tree sort. It also serves the gap finder's
  timestamp list and the supervisor's resume-gap lookup.
* ``ix_frame_project_state_origin_ts_size`` on ``(project_id, lifecycle_state,
  origin, capture_timestamp, file_size_bytes)``. It covers the dashboard's one
  grouped aggregate (newest captured timestamp and summed size of the active
  frames), so that query never reads a table row.

Index-only: no table is rebuilt and no row is touched, so no foreign key can
cascade. Both are mirrored in ``Frame.__table_args__`` so the model and the
schema agree.

Revision ID: 035_frame_read_path_indexes
Revises: 034_exact_time_fire_retry
Create Date: 2026-10-28

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "035_frame_read_path_indexes"
down_revision: str | None = "034_exact_time_fire_retry"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_frame_project_ts",
        "frame",
        ["project_id", "capture_timestamp", "sequence_index"],
    )
    op.create_index(
        "ix_frame_project_state_origin_ts_size",
        "frame",
        [
            "project_id",
            "lifecycle_state",
            "origin",
            "capture_timestamp",
            "file_size_bytes",
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_frame_project_state_origin_ts_size", table_name="frame")
    op.drop_index("ix_frame_project_ts", table_name="frame")
