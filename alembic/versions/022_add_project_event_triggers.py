"""add project event triggers

Introduces event-triggered capture: a project may carry a list of trigger
objects, each naming a camera event topic, an enable flag, and a per-trigger
debounce cooldown. A matching camera event captures a single frame, independent
of the recurring capture-gating schedule.

* New ``project.event_triggers`` column (nullable JSON, a list of trigger
  objects). Only selected triggers are persisted; discovery of the camera's full
  event catalogue is on-demand, so no camera-table migration is needed.

The column is additive and nullable, so every pre-existing row reads back as
``NULL`` (no triggers, no event listener), preserving today's behaviour.

Revision ID: 022_add_project_event_triggers
Revises: 021_add_exact_time_fire_table
Create Date: 2026-06-23

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "022_add_project_event_triggers"
down_revision: str | None = "021_add_exact_time_fire_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.add_column(sa.Column("event_triggers", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.drop_column("event_triggers")
