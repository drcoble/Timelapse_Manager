"""add user preference columns

Adds two per-user display-preference columns to ``user``:

* ``theme_preference`` -- the user's preferred colour scheme:
  ``"light"``, ``"dark"``, or ``"system"`` (follow the OS).
  Stored as a non-nullable string; existing rows default to ``"system"``.
* ``viewer_timezone`` -- an IANA timezone name used to localise timestamps
  shown in the UI (e.g. ``"America/New_York"``). Nullable; a null value
  means fall back to UTC display.

Revision ID: 006_add_user_preferences
Revises: 005_add_project_campaign_bounds
Create Date: 2026-06-15

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "006_add_user_preferences"
down_revision: str | None = "005_add_project_campaign_bounds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(
            sa.Column(
                "theme_preference",
                sa.String(),
                nullable=False,
                server_default="system",
            )
        )
        batch_op.add_column(
            sa.Column("viewer_timezone", sa.String(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("viewer_timezone")
        batch_op.drop_column("theme_preference")
