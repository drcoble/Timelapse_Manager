"""notification delivery cursor

Issue #67 (R8): the notification dispatcher started from the newest event id on
every boot, so anything logged while it was stopped, and any send cut off by a
shutdown, was never delivered. A new singleton table ``notification_cursor``
holds its persisted position:

* ``last_event_id``: the delivery cursor (every event at or below it settled);
* ``last_read_event_id``: the read mark (every event at or below it has been
  through the active-alert auto-clear, which must not be replayed).

The table starts empty. On the first boot after the upgrade the dispatcher
inserts the row at the current newest event id, which is the old behaviour, so
an upgrade never floods the channels with history.

A new table only: no existing table is rebuilt and no row is touched, so no
foreign key can cascade. Mirrored by ``NotificationCursor``.

Revision ID: 036_notification_cursor
Revises: 035_frame_read_path_indexes
Create Date: 2026-10-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "036_notification_cursor"
down_revision: str | None = "035_frame_read_path_indexes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notification_cursor",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("last_event_id", sa.Integer(), nullable=False),
        sa.Column("last_read_event_id", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_notification_cursor_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("notification_cursor")
