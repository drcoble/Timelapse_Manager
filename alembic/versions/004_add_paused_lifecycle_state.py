"""add paused lifecycle state

Widens the project ``lifecycle_state`` enum from ``(active, archived)`` to
``(active, paused, archived)`` so a single project's capture can be paused at
runtime (the capture supervisor stops a project's loop as soon as it leaves the
``active`` state).

The enum is portable (``native_enum=False``), so on SQLite it is a ``VARCHAR``
guarded by a named ``CHECK (lifecycle_state IN (...))`` constraint rather than a
real database enum type. SQLite cannot ``ALTER`` a CHECK constraint in place, so
the column is rewritten via :func:`op.batch_alter_table` (alembic's table-rebuild
mode): the table is copied into a new one carrying the widened CHECK. The
downgrade narrows the CHECK back to ``(active, archived)``; any rows left in the
``paused`` state are first folded back to ``active`` so the tighter constraint
the rebuild applies cannot be violated.

Revision ID: 004_add_paused_lifecycle_state
Revises: 003_session_auth_columns
Create Date: 2026-06-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "004_add_paused_lifecycle_state"
down_revision: str | None = "003_session_auth_columns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NAME = "project_lifecycle_state"


def _enum(*values: str) -> sa.Enum:
    """Build the portable (CHECK-constraint) lifecycle enum for ``values``."""
    return sa.Enum(*values, name=_NAME, native_enum=False)


def upgrade() -> None:
    with op.batch_alter_table("project") as batch_op:
        batch_op.alter_column(
            "lifecycle_state",
            existing_type=_enum("active", "archived"),
            type_=_enum("active", "paused", "archived"),
            existing_nullable=False,
        )


def downgrade() -> None:
    # Fold any paused rows back to active before tightening the CHECK, so the
    # table-copy's narrower constraint cannot reject existing data.
    op.execute(
        "UPDATE project SET lifecycle_state = 'active' "
        "WHERE lifecycle_state = 'paused'"
    )
    with op.batch_alter_table("project") as batch_op:
        batch_op.alter_column(
            "lifecycle_state",
            existing_type=_enum("active", "paused", "archived"),
            type_=_enum("active", "archived"),
            existing_nullable=False,
        )
