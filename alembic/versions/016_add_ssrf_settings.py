"""add SSRF settings allow-list table

Introduces a place for an administrator to widen the SSRF camera/scan opt-in
allow-list from the web UI (previously only the config file / ``TLM_SSRF__*``
environment variable could set it):

* New singleton table ``ssrf_settings`` (its primary key is constrained to
  ``1``):
  - ``allowed_private_subnets`` -- a JSON list of CIDR strings an admin has
    opted into. Additive to any subnets provided by config/env; the union is
    applied to the running policy at startup and on save. Nullable; ``NULL`` (or
    an absent row) means "no admin-added subnets", so an upgrade changes nothing
    until an admin saves one.
  - ``created_at`` / ``updated_at`` -- database-managed timestamps.

Purely additive; no data is migrated.

Revision ID: 016_add_ssrf_settings
Revises: 015_add_frame_excluded_at
Create Date: 2026-06-21

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "016_add_ssrf_settings"
down_revision: str | None = "015_add_frame_excluded_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ssrf_settings",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("allowed_private_subnets", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.current_timestamp(),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_ssrf_settings_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("ssrf_settings")
