"""session auth columns

Adds the columns the web login layer needs to the ``session`` table:

* ``token_hash`` -- the SHA-256 hash of the session's raw cookie token. Stored
  hashed so a database leak discloses no usable credential. Added nullable (an
  additive ``ALTER TABLE`` on SQLite cannot supply a per-row default for an
  existing population) and backed by a *unique* index; SQLite treats NULLs as
  distinct, so any pre-existing rows without a hash do not collide. New rows
  written by the application always populate it.
* ``csrf_secret`` -- per-session secret backing the synchronizer CSRF token.
* ``last_active`` -- timestamp the idle-timeout clock is measured from.

Revision ID: 003_session_auth_columns
Revises: 002_create_core_schema
Create Date: 2026-06-10

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "003_session_auth_columns"
down_revision: str | None = "002_create_core_schema"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("session", sa.Column("token_hash", sa.String(), nullable=True))
    op.add_column("session", sa.Column("csrf_secret", sa.String(), nullable=True))
    op.add_column("session", sa.Column("last_active", sa.DateTime(), nullable=True))
    op.create_index(
        "ix_session_token_hash", "session", ["token_hash"], unique=True
    )


def downgrade() -> None:
    op.drop_index("ix_session_token_hash", table_name="session")
    op.drop_column("session", "last_active")
    op.drop_column("session", "csrf_secret")
    op.drop_column("session", "token_hash")
