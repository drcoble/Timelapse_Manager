"""add login-attempt table

Creates the durable backing store for brute-force login throttling so the
throttle decision survives a process restart and cannot be reset by clearing
in-memory state.

* New ``login_attempt`` table:
  - ``ip`` -- the effective client IP the attempt came from (reverse-proxy aware).
  - ``username`` -- the username string exactly as submitted (no foreign key: an
    attempt for a non-existent account must still be recorded, and a key would
    leak account existence).
  - ``attempted_at`` -- when the attempt was made (naive UTC).
  - ``succeeded`` -- whether it authenticated.
  - Indexes ``ix_login_attempt_ip_attempted_at`` on ``(ip, attempted_at)`` and
    ``ix_login_attempt_username_attempted_at`` on ``(username, attempted_at)``
    to make the per-IP / per-username sliding-window counts cheap.

The table has no foreign keys, so dropping it on downgrade is a plain
``drop_table`` with no foreign-key-suspend dance.

Revision ID: 024_add_login_attempt
Revises: 023_add_notification_webhook_signing_secret
Create Date: 2026-06-30

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "024_add_login_attempt"
down_revision: str | None = "023_add_notification_webhook_signing_secret"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_attempt",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ip", sa.String(), nullable=False),
        sa.Column("username", sa.String(), nullable=False),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
        sa.Column("succeeded", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_login_attempt_ip_attempted_at",
        "login_attempt",
        ["ip", "attempted_at"],
    )
    op.create_index(
        "ix_login_attempt_username_attempted_at",
        "login_attempt",
        ["username", "attempted_at"],
    )
    op.create_index(
        "ix_login_attempt_attempted_at",
        "login_attempt",
        ["attempted_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_login_attempt_attempted_at", table_name="login_attempt")
    op.drop_index("ix_login_attempt_username_attempted_at", table_name="login_attempt")
    op.drop_index("ix_login_attempt_ip_attempted_at", table_name="login_attempt")
    op.drop_table("login_attempt")
