"""add case-insensitive user identity (username_key)

Issue #4: usernames were compared case-sensitively, so one directory account
logged in as ``Alice.smith`` and later ``alice.smith`` became two ``user`` rows
(each with the mapped role). This migration adds the comparison key the
application now uses for every lookup:

* New ``user.username_key`` (NOT NULL) = ``username`` stripped, NFKC-normalised
  and casefolded (:func:`timelapse_manager.usernames.username_key`), with the
  unique index ``ix_user_username_key``.

Existing rows whose keys collide are resolved before the index is created:

* **Directory (LDAP) duplicates are merged** into the oldest row (lowest id). It
  takes the highest role among them and stays enabled only if every duplicate
  was enabled. Every foreign key to ``user.id`` (``event.actor_user_id``,
  ``event.alert_cleared_by``, ``milestone.user_id``, ``session.user_id``) is
  re-pointed to it, then the extra rows are deleted. The stored spelling is
  refreshed to the directory's own value at that person's next login.
* **Local duplicates, or a local account colliding with a directory one, stop
  the upgrade** with an error naming the accounts: which password should win is
  an administrator's decision. Rename or delete one of them (on the previous
  version) and run the upgrade again.

Downgrade drops the index and the column; merged rows are not restored.

Revision ID: 025_add_user_username_key
Revises: 024_add_login_attempt
Create Date: 2026-10-15

"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from timelapse_manager.db.migration_utils import suspend_foreign_keys
from timelapse_manager.usernames import username_key

# revision identifiers, used by Alembic.
revision: str = "025_add_user_username_key"
down_revision: str | None = "024_add_login_attempt"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_ROLE_RANK = {"viewer": 0, "operator": 1, "admin": 2}
# Every foreign key to user.id in the schema at 024 (table, column).
_USER_FKS = (
    ("event", "actor_user_id"),
    ("event", "alert_cleared_by"),
    ("milestone", "user_id"),
    ("session", "user_id"),
)


class DuplicateLocalUsernameError(RuntimeError):
    """Raised when local accounts differ only by case/whitespace."""


def _resolve_duplicates(bind: sa.engine.Connection) -> None:
    rows = bind.execute(
        sa.text(
            'SELECT id, username, auth_source, role, enabled FROM "user" ORDER BY id'
        )
    ).all()
    groups: dict[str, list[sa.Row]] = defaultdict(list)
    for row in rows:
        groups[username_key(row.username)].append(row)

    conflicts = [g for g in groups.values() if len(g) > 1]
    blocking = [g for g in conflicts if any(r.auth_source != "ldap" for r in g)]
    if blocking:
        listed = "; ".join(
            ", ".join(f"{r.username!r} ({r.auth_source}, id {r.id})" for r in g)
            for g in blocking
        )
        raise DuplicateLocalUsernameError(
            "migration 025 stopped: these accounts differ only by letter case or "
            f"surrounding spaces and at least one is a local account: {listed}. "
            "Rename or delete the extra account(s), then upgrade again."
        )

    for group in conflicts:
        keeper, extras = group[0], group[1:]
        extra_ids = [r.id for r in extras]
        role = max((r.role for r in group), key=lambda value: _ROLE_RANK.get(value, 0))
        enabled = all(bool(r.enabled) for r in group)
        params = {"keeper": keeper.id, "extras": extra_ids}
        for table, column in _USER_FKS:
            bind.execute(
                sa.text(
                    f'UPDATE "{table}" SET {column} = :keeper WHERE {column} IN :extras'
                ).bindparams(sa.bindparam("extras", expanding=True)),
                params,
            )
        bind.execute(
            sa.text(
                'UPDATE "user" SET role = :role, enabled = :enabled WHERE id = :keeper'
            ),
            {"role": role, "enabled": enabled, "keeper": keeper.id},
        )
        bind.execute(
            sa.text('DELETE FROM "user" WHERE id IN :extras').bindparams(
                sa.bindparam("extras", expanding=True)
            ),
            params,
        )
        logger.warning(
            "merged directory accounts %s into id %s %r (role %s, enabled %s)",
            ", ".join(f"{r.username!r} (id {r.id})" for r in extras),
            keeper.id,
            keeper.username,
            role,
            enabled,
        )


def upgrade() -> None:
    # The NOT NULL change rebuilds ``user`` on SQLite; the implicit DROP would
    # cascade through ``session.user_id`` (ON DELETE CASCADE). Enforcement is
    # suspended first -- before any DML -- as the helper requires.
    with suspend_foreign_keys(op):
        bind = op.get_bind()
        # Resolve (or refuse) duplicates before any DDL: SQLite DDL is not
        # transactional here, so a refusal must leave the schema exactly at 024
        # for a clean retry.
        _resolve_duplicates(bind)

        with op.batch_alter_table("user") as batch_op:
            batch_op.add_column(sa.Column("username_key", sa.String(), nullable=True))

        for row in bind.execute(sa.text('SELECT id, username FROM "user"')).all():
            bind.execute(
                sa.text('UPDATE "user" SET username_key = :key WHERE id = :id'),
                {"key": username_key(row.username), "id": row.id},
            )

        with op.batch_alter_table("user") as batch_op:
            batch_op.alter_column(
                "username_key", existing_type=sa.String(), nullable=False
            )
            batch_op.create_index("ix_user_username_key", ["username_key"], unique=True)


def downgrade() -> None:
    with suspend_foreign_keys(op), op.batch_alter_table("user") as batch_op:
        batch_op.drop_index("ix_user_username_key")
        batch_op.drop_column("username_key")
