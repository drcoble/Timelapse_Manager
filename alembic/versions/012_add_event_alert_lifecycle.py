"""add event active-alert lifecycle columns

Adds the columns that turn the existing append-only ``event`` log into the
backing store for an in-UI active-alerts view, without a separate table:

* ``event.alert_cleared_at`` (nullable datetime) -- when an alert left the
  active list. ``NULL`` means the alert is still active. Every existing row
  defaults to ``NULL``, so all historical warning/error/critical events become
  active alerts on upgrade (they are the current outstanding conditions).
* ``event.alert_cleared_by`` (nullable FK to ``user.id``, ``ON DELETE SET
  NULL``) -- the user who manually cleared the alert. ``NULL`` when the alert
  was auto-cleared by a resolve signal (or is still active).
* ``event.alert_clear_reason`` (nullable string) -- ``"manual"`` or ``"auto"``;
  ``NULL`` while the alert is active.

All three are purely additive and nullable; clearing an alert only updates these
columns and never deletes the event row, so the operational log stays complete.

Revision ID: 012_add_event_alert_lifecycle
Revises: 011_add_frame_scene_metadata
Create Date: 2026-06-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "012_add_event_alert_lifecycle"
down_revision: str | None = "011_add_frame_scene_metadata"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("event") as batch_op:
        batch_op.add_column(
            sa.Column("alert_cleared_at", sa.DateTime(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("alert_cleared_by", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("alert_clear_reason", sa.String(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_event_alert_cleared_by_user",
            "user",
            ["alert_cleared_by"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    with op.batch_alter_table("event") as batch_op:
        batch_op.drop_constraint(
            "fk_event_alert_cleared_by_user", type_="foreignkey"
        )
        batch_op.drop_column("alert_clear_reason")
        batch_op.drop_column("alert_cleared_by")
        batch_op.drop_column("alert_cleared_at")
