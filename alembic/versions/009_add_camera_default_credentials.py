"""add default camera credentials

Introduces a global fallback login for credential-free cameras, opt-in per
camera:

* New singleton table ``camera_default_credentials`` (its primary key is
  constrained to ``1``):
  - ``enabled`` -- the master switch. Non-nullable; defaults to off so an
    upgrade never silently enables a fallback.
  - ``username`` -- the fallback username (nullable; non-secret, stored verbatim).
  - ``password`` -- the fallback password (nullable). Stored encrypted via the
    at-rest helpers (``enc:v1:`` prefix), masked on read, masked write-back on
    update -- the same pattern the directory bind password uses.
  - ``created_at`` / ``updated_at`` -- database-managed timestamps.

* New ``camera.credentials_inherit_default`` column -- whether a camera with no
  credentials of its own falls back to the global default. Non-nullable with a
  ``0`` server default, so every existing row is off until an operator opts it
  in; cameras created through the application default to on (the ORM column
  default).

Revision ID: 009_add_camera_default_credentials
Revises: 008_add_ldap_tls_ca_cert
Create Date: 2026-06-17

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "009_add_camera_default_credentials"
down_revision: str | None = "008_add_ldap_tls_ca_cert"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "camera_default_credentials",
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("username", sa.String(), nullable=True),
        sa.Column("password", sa.String(), nullable=True),
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
        sa.CheckConstraint("id = 1", name="ck_camera_default_credentials_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("camera") as batch_op:
        batch_op.add_column(
            sa.Column(
                "credentials_inherit_default",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("camera") as batch_op:
        batch_op.drop_column("credentials_inherit_default")
    op.drop_table("camera_default_credentials")
