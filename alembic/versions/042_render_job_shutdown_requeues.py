"""render job shutdown requeues

Issue #178: a render interrupted by a clean service stop is put back to
``pending`` once, so a restart (an upgrade, a reboot) does not cost the user a
render. To requeue it only once -- never in a loop if the render itself is what
stops the service -- the job remembers how many times a shutdown requeued it:

* ``render_job.shutdown_requeues`` INTEGER NOT NULL, server default ``0``.

Every existing row reads ``0``, so no job's state changes on upgrade and no data
is rewritten.

Native ``ALTER TABLE ... ADD COLUMN``: the ``render_job`` table is not rebuilt,
so no ``ON DELETE CASCADE`` can fire. The downgrade is a native ``DROP COLUMN``
(SQLite 3.35 or later; the frozen build bundles 3.53.1).

Revision ID: 042_render_job_shutdown_requeues
Revises: 041_project_active_since
Create Date: 2026-10-29

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "042_render_job_shutdown_requeues"
down_revision: str | None = "041_project_active_since"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "render_job",
        sa.Column(
            "shutdown_requeues",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("render_job", "shutdown_requeues")
