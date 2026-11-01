"""add report.anonymized

Issue #21 (D15): a problem report can be filed anonymized -- every report is
when the GitHub hand-off is enabled. Its stored zip, title and description are
then the anonymized ones, and the reporter may download that zip from the
result screen. The new column records which reports qualify, so the reporter
download and the admin list need not open the zip:

* New ``report.anonymized`` (Boolean, NOT NULL, server default false). Every
  pre-existing report was filed un-anonymized and reads back as false.

Both directions may make SQLite rebuild ``report``; the rebuild keeps
``AUTOINCREMENT`` (029) and runs with foreign-key enforcement suspended.

Revision ID: 030_add_report_anonymized
Revises: 029_report_autoincrement
Create Date: 2026-10-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from timelapse_manager.db.migration_utils import suspend_foreign_keys

# revision identifiers, used by Alembic.
revision: str = "030_add_report_anonymized"
down_revision: str | None = "029_report_autoincrement"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Batch mode may rebuild ``report`` on SQLite; reflection does not carry
    # the AUTOINCREMENT table option, so it is restated (029 must survive).
    with (
        suspend_foreign_keys(op),
        op.batch_alter_table(
            "report", table_kwargs={"sqlite_autoincrement": True}
        ) as batch_op,
    ):
        batch_op.add_column(
            sa.Column(
                "anonymized",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with (
        suspend_foreign_keys(op),
        op.batch_alter_table(
            "report",
            recreate="always",
            table_kwargs={"sqlite_autoincrement": True},
        ) as batch_op,
    ):
        batch_op.drop_column("anonymized")
