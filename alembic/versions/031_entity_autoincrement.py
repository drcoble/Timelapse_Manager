"""project/camera/milestone/frame/user ids never reused (AUTOINCREMENT)

Issue #17, the entity counterpart of 028/029: ``user``, ``camera``,
``project``, ``milestone`` and ``frame`` used a plain SQLite rowid, so after the
highest-id row was deleted the next row got the same id. Events are scoped by
project and camera id and the audit log names projects, cameras, milestones and
frames by id ("project 7 deleted", "frame 812 soft-deleted"), so a new row
inherited the history of the deleted one.

* The five tables are rebuilt with ``AUTOINCREMENT``: SQLite then keeps the
  highest id ever handed out in ``sqlite_sequence`` and never reuses it.
* Each table's ``sqlite_sequence`` row is seeded with the highest id known for
  it: the table's own ``max(id)``, the columns that reference it (including
  ``SET NULL`` links that may still hold a since-deleted id, and
  ``event.scope_id`` for project/camera-scoped events), the ``frame_id``
  metadata of frame audit events, and the id in audit messages the application
  itself writes in a fixed form ("project N created", "frame N restored", ...).
  Ids of rows already deleted before this migration, if anything still records
  them, are therefore not handed out again either.
* Only those structured sources are read. Messages are matched anchored at both
  ends, so a project *named* "project 5 created" inside a longer message
  supplies nothing, and any candidate more than :data:`_MAX_JUMP` above the
  table's own ``max(id)`` is ignored with a warning, so the log can never push a
  counter towards the 64-bit limit.

Unlike ``render_job``, these are parent tables: ``project`` and ``camera`` are
referenced by ``CASCADE``, ``RESTRICT`` and ``SET NULL`` links, ``frame`` by
``exact_time_fire`` and ``user`` by ``session``, ``milestone``, ``event`` and
``report``. All five rebuilds therefore run inside one
:func:`suspend_foreign_keys` block, which is the first thing the step does, so
the implicit ``DROP TABLE`` of each rebuild fires none of those actions.
``PRAGMA foreign_key_check`` runs before the rebuilds (a database that already
has dangling references is refused untouched) and again after them; any row
it reports raises, so a rebuild that broke a link cannot be committed.

Downgrade rebuilds the five tables without ``AUTOINCREMENT`` and deletes their
``sqlite_sequence`` rows; ids may then be reused again.

Revision ID: 031_entity_autoincrement
Revises: 030_add_report_anonymized
Create Date: 2026-10-23

"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from timelapse_manager.db.migration_utils import suspend_foreign_keys

# revision identifiers, used by Alembic.
revision: str = "031_entity_autoincrement"
down_revision: str | None = "030_add_report_anonymized"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# Rebuild order: the tables nothing here depends on come first.
_TABLES = ("user", "camera", "project", "milestone", "frame")

# A remembered id further than this above the table's own max(id) is not
# believable (it would take a million deleted rows) and is ignored.
_MAX_JUMP = 1_000_000

# Columns holding an id of each table, written by the application, never typed
# by a user. Static SQL only: no part of these statements comes from data.
_COLUMN_SOURCES: dict[str, tuple[str, ...]] = {
    "user": (
        "SELECT max(actor_user_id) FROM event",
        "SELECT max(alert_cleared_by) FROM event",
        "SELECT max(user_id) FROM milestone",
        "SELECT max(user_id) FROM report",
        "SELECT max(user_id) FROM session",
    ),
    "camera": (
        "SELECT max(scope_id) FROM event WHERE scope = 'camera'",
        "SELECT max(camera_id) FROM report",
    ),
    "project": (
        "SELECT max(scope_id) FROM event WHERE scope = 'project'",
        "SELECT max(project_id) FROM report",
    ),
    "milestone": (),
    "frame": ("SELECT max(frame_id) FROM exact_time_fire",),
}

# Frame audit events carry the frame id in metadata, next to an ``action`` the
# application sets itself (``storage/frames.py``).
_FRAME_EVENT_ACTIONS = (
    "soft_delete",
    "restore",
    "exclude",
    "include",
    "permanent_delete",
    "edit_capture_timestamp",
    "upload",
    "import",
)

# The audit messages the application writes about each entity, anchored at
# both ends: the id comes first and the message has no user-influenced text, so
# nothing a user typed can supply an id. The value is (event scope, prefix the
# message starts with, patterns).
_MESSAGE_SOURCES: dict[str, tuple[str, str, tuple[re.Pattern[str], ...]]] = {
    "camera": (
        "camera",
        "camera ",
        (re.compile(r"\Acamera (\d{1,18}) (?:created|updated|deleted)\Z"),),
    ),
    "project": (
        "project",
        "project ",
        (
            re.compile(
                r"\Aproject (\d{1,18}) (?:created|updated|deleted|paused|resumed)\Z"
            ),
            re.compile(r"\Aproject (\d{1,18}) cloned from \d{1,18}\Z"),
        ),
    ),
    "milestone": (
        "project",
        "milestone ",
        (re.compile(r"\Amilestone (\d{1,18}) (?:created|updated|deleted)\Z"),),
    ),
    "frame": (
        "project",
        "frame ",
        (
            re.compile(
                r"\Aframe (\d{1,18}) (?:soft-deleted|restored|permanently deleted"
                r"|excluded from render|included in render"
                r"|capture timestamp edited|imported|uploaded)\Z"
            ),
        ),
    ),
}


def _is_id(value: object) -> bool:
    """True for a positive integer (``bool`` excluded) usable as a row id."""
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _ids_recorded_for(bind: sa.engine.Connection, table: str) -> list[int]:
    """Ids of ``table`` named by referencing columns, metadata and audit text."""
    ids: list[int] = []
    for statement in _COLUMN_SOURCES[table]:
        value = bind.execute(sa.text(statement)).scalar()
        if _is_id(value):
            ids.append(value)
    if table == "frame":
        actions = {f"a{n}": action for n, action in enumerate(_FRAME_EVENT_ACTIONS)}
        placeholders = ", ".join(f":{name}" for name in actions)
        rows = bind.execute(
            sa.text(
                "SELECT json_extract(metadata, '$.frame_id') FROM event "
                "WHERE scope = 'project' AND json_valid(metadata) "
                f"AND json_extract(metadata, '$.action') IN ({placeholders})"
            ),
            actions,
        ).scalars()
        ids += [value for value in rows if _is_id(value)]
    if table in _MESSAGE_SOURCES:
        scope, prefix, patterns = _MESSAGE_SOURCES[table]
        messages = bind.execute(
            sa.text(
                "SELECT message FROM event WHERE scope = :scope "
                "AND substr(message, 1, :length) = :prefix"
            ),
            {"scope": scope, "length": len(prefix), "prefix": prefix},
        ).scalars()
        for message in messages:
            for pattern in patterns:
                match = pattern.match(message or "")
                if match:
                    ids.append(int(match.group(1)))
                    break
    return ids


def _highest_known_id(bind: sa.engine.Connection, table: str) -> int:
    """The highest id of ``table`` in the table or believably recorded elsewhere."""
    highest_row = bind.execute(sa.text(f'SELECT max(id) FROM "{table}"')).scalar() or 0
    ceiling = highest_row + _MAX_JUMP
    highest = highest_row
    for row_id in _ids_recorded_for(bind, table):
        if row_id > ceiling:
            logger.warning(
                "ignoring %s id %d recorded in the database: more than %d above "
                "the highest existing id %d",
                table,
                row_id,
                _MAX_JUMP,
                highest_row,
            )
            continue
        highest = max(highest, row_id)
    return highest


def _seed_sequence(bind: sa.engine.Connection, table: str, floor: int) -> None:
    """Raise ``table``'s ``sqlite_sequence`` row to at least ``floor``."""
    # The row copy already set the sequence to max(id); raise it to cover ids
    # recorded elsewhere. An empty table has no sequence row yet.
    updated = bind.execute(
        sa.text("UPDATE sqlite_sequence SET seq = max(seq, :floor) WHERE name = :name"),
        {"floor": floor, "name": table},
    ).rowcount
    if not updated and floor:
        bind.execute(
            sa.text("INSERT INTO sqlite_sequence (name, seq) VALUES (:name, :floor)"),
            {"name": table, "floor": floor},
        )


def _require_no_dangling_references(bind: sa.engine.Connection, when: str) -> None:
    """Raise if ``PRAGMA foreign_key_check`` reports any row."""
    violations = bind.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if violations:
        shown = ", ".join(
            f"{row[0]} rowid {row[1]} -> {row[2]}" for row in violations[:10]
        )
        raise RuntimeError(
            f"PRAGMA foreign_key_check reported {len(violations)} dangling "
            f"reference(s) {when}; refusing to continue: {shown}"
        )


def _rebuild(table: str, *, autoincrement: bool) -> None:
    """Recreate ``table`` with or without ``AUTOINCREMENT``, keeping its rows."""
    with op.batch_alter_table(
        table,
        recreate="always",
        table_kwargs={"sqlite_autoincrement": autoincrement},
    ):
        pass


def upgrade() -> None:
    with suspend_foreign_keys(op):
        bind = op.get_bind()
        _require_no_dangling_references(bind, "before the rebuild")
        for table in _TABLES:
            _rebuild(table, autoincrement=True)
        _require_no_dangling_references(bind, "after the rebuild")
        for table in _TABLES:
            _seed_sequence(bind, table, _highest_known_id(bind, table))


def downgrade() -> None:
    with suspend_foreign_keys(op):
        bind = op.get_bind()
        for table in _TABLES:
            _rebuild(table, autoincrement=False)
        _require_no_dangling_references(bind, "after the rebuild")
        for table in _TABLES:
            bind.execute(
                sa.text("DELETE FROM sqlite_sequence WHERE name = :name"),
                {"name": table},
            )
