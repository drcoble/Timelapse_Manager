"""project active_since

Issue #280: the exact-time catch-up recorded a ``skipped_missed`` row and a
warning for anchor instants from before a project existed, or while it was
paused. To tell those from a genuine miss the runner needs the moment the
project last became active:

* ``project.active_since`` DATETIME NULL -- naive UTC, set whenever the project's
  ``lifecycle_state`` becomes ``active`` (create, resume, reactivate, start).

Existing rows read ``NULL``, which the runner treats as "since ``created_at``",
so no project's behaviour changes on upgrade and no data is rewritten.

Native ``ALTER TABLE ... ADD COLUMN``: the ``project`` table is not rebuilt, so no
``ON DELETE CASCADE`` from it can fire. The downgrade is a native ``DROP COLUMN``
(SQLite 3.35 or later; the frozen build bundles 3.53.1).

Revision ID: 041_project_active_since
Revises: 040_secrets_meta
Create Date: 2026-10-29

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "041_project_active_since"
down_revision: str | None = "040_secrets_meta"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project", sa.Column("active_since", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("project", "active_since")
