"""add ldap auth master switch and membership mode

Adds two columns to ``ldap_settings`` to complete the directory-integration
configuration introduced as a stub in the core-schema migration:

* ``enabled`` -- the master switch for directory authentication. Non-nullable;
  existing rows default to ``false`` (off) so an upgrade never silently turns
  LDAP on. The connector refuses to run while this is false.
* ``membership_mode`` -- how a user's group memberships are discovered:
  ``"memberof"`` reads the user entry's ``memberOf`` attribute; ``"group_search"``
  searches group entries whose member attribute references the user's DN.
  Non-nullable; existing rows default to ``"memberof"``.
* ``group_search_base`` -- the subtree the ``"group_search"`` mode searches for
  group entries. Nullable; groups commonly live outside the user subtree, so when
  unset the connector falls back to the directory suffix of the user search base.

The recursion axis (direct vs nested/transitive group evaluation) is the
pre-existing ``nested_groups`` column and is intentionally left untouched -- it is
orthogonal to the discovery mode this migration adds.

Revision ID: 007_add_ldap_auth_columns
Revises: 006_add_user_preferences
Create Date: 2026-06-16

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "007_add_ldap_auth_columns"
down_revision: str | None = "006_add_user_preferences"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The membership-discovery mode enum, kept in sync with the model's
# ``_membership_mode_enum``. ``native_enum=False`` renders as a CHECK-constrained
# VARCHAR on SQLite, matching how the other string enums in this schema are
# emitted.
_membership_mode_enum = sa.Enum(
    "memberof",
    "group_search",
    name="ldap_membership_mode",
    native_enum=False,
)


def upgrade() -> None:
    with op.batch_alter_table("ldap_settings") as batch_op:
        batch_op.add_column(
            sa.Column(
                "enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "membership_mode",
                _membership_mode_enum,
                nullable=False,
                server_default="memberof",
            )
        )
        batch_op.add_column(
            sa.Column("group_search_base", sa.String(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("ldap_settings") as batch_op:
        batch_op.drop_column("group_search_base")
        batch_op.drop_column("membership_mode")
        batch_op.drop_column("enabled")
