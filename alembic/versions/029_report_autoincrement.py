"""report ids never reused (AUTOINCREMENT)

Issue #9 follow-up, the problem-report counterpart of 028: ``report.id`` was a
plain SQLite rowid, so deleting the newest report let the next one take its id.
``report`` is rebuilt with ``AUTOINCREMENT``; the row copy leaves
``sqlite_sequence`` at the table's ``max(id)``.

The user-visible key (``TLM-YYYYMMDD-NNNN``) is not a column default and needs
no migration: the application now allocates it past every key of that day the
table or the report audit log has ever held (see
``timelapse_manager.reports.store.next_report_key``).

The rebuild runs with foreign-key enforcement suspended; nothing references
``report``, and its own ``ON DELETE SET NULL`` links are kept.

Downgrade rebuilds the table without ``AUTOINCREMENT`` and drops its
``sqlite_sequence`` row.

Revision ID: 029_report_autoincrement
Revises: 028_render_job_autoincrement
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

from timelapse_manager.db.migration_utils import suspend_foreign_keys

# revision identifiers, used by Alembic.
revision: str = "029_report_autoincrement"
down_revision: str | None = "028_render_job_autoincrement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with (
        suspend_foreign_keys(op),
        op.batch_alter_table(
            "report",
            recreate="always",
            table_kwargs={"sqlite_autoincrement": True},
        ),
    ):
        pass


def downgrade() -> None:
    with suspend_foreign_keys(op):
        with op.batch_alter_table(
            "report",
            recreate="always",
            table_kwargs={"sqlite_autoincrement": False},
        ):
            pass
        op.execute("DELETE FROM sqlite_sequence WHERE name = 'report'")
