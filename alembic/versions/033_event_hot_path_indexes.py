"""event hot-path indexes

Issue #66 (C4): the hot event reads and the auto-clear UPDATE scanned the whole
``event`` table, which only grows. Two plain indexes bound them:

* ``ix_event_open_alerts`` on ``(level, timestamp)``, **partial** on the open
  alerts: ``alert_cleared_at IS NULL AND level IN ('warning', 'error',
  'critical')``. It serves the active-alert count and page
  (``get_active_alerts``), which repeat that predicate with literal values so
  SQLite can prove the index applies. The level term keeps the uncleared
  ``info`` rows (most of the log) out of the index: without it, ``ANALYZE``
  statistics average the index over those rows and the planner falls back to a
  full scan.
* ``ix_event_scope_scope_id`` on ``(scope, scope_id, id)``. It bounds the
  resolve-signal auto-clear UPDATE to one entity's events, and serves the
  per-entity reads (report snapshot, events page).

Index-only: no table is rebuilt and no row is touched, so no foreign key can
cascade. Both are mirrored in ``Event.__table_args__`` so the model and the
schema agree.

Revision ID: 033_event_hot_path_indexes
Revises: 032_render_job_codec
Create Date: 2026-10-27

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "033_event_hot_path_indexes"
down_revision: str | None = "032_render_job_codec"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_event_open_alerts",
        "event",
        ["level", "timestamp"],
        # Frozen copy of ``db.models.event.OPEN_ALERT_INDEX_WHERE``.
        sqlite_where=sa.text(
            "alert_cleared_at IS NULL AND level IN ('warning', 'error', 'critical')"
        ),
    )
    op.create_index(
        "ix_event_scope_scope_id",
        "event",
        ["scope", "scope_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_event_scope_scope_id", table_name="event")
    op.drop_index("ix_event_open_alerts", table_name="event")
