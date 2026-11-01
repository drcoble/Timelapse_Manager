"""render_job ids never reused (AUTOINCREMENT)

Issue #9 follow-up: ``render_job.id`` was a plain SQLite rowid, so after the
highest-id job was deleted the next job got the same id. The audit log names
jobs by id ("manual render 2 triggered", "render 2 deleted by ..."), and the
render/export cards find a job's failure reason and trigger user through those
events, so a new job could show a deleted job's failure reason or trigger user.

* ``render_job`` is rebuilt with ``AUTOINCREMENT``: SQLite then keeps the
  highest id ever handed out in ``sqlite_sequence`` and never reuses it.
* ``sqlite_sequence`` is seeded with the highest job id known: the table's own
  ``max(id)``, the ``render_id`` metadata of ``render.failed`` and
  ``export.complete`` events, and the job id in messages the application itself
  writes in a fixed form ("manual render N triggered", "render N deleted by
  ...", ...). Ids of jobs already deleted before this migration, if the log
  records them, are therefore not handed out again either.
* Only those structured sources are read -- never a number found somewhere in
  free text, which can carry user input (project names, reasons) -- and any
  candidate more than :data:`_MAX_JUMP` above ``max(id)`` is ignored with a
  warning, so the log can never push the counter towards the 64-bit limit.

The rebuild runs with foreign-key enforcement suspended, like every other
table rebuild here; nothing references ``render_job``, and ``render_job`` keeps
its own ``ON DELETE CASCADE`` to ``project``.

Downgrade rebuilds the table without ``AUTOINCREMENT`` and drops its
``sqlite_sequence`` row; ids may then be reused again.

Revision ID: 028_render_job_autoincrement
Revises: 027_add_report_table
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
revision: str = "028_render_job_autoincrement"
down_revision: str | None = "027_add_report_table"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# The messages the application writes about a job, anchored at the start: the
# id always comes first, before any user-influenced text (a username, a
# failure reason), so nothing a user typed can supply it.
_JOB_MESSAGES = (
    re.compile(r"\Amanual render (\d{1,18}) triggered\Z"),
    re.compile(r"\Aexport (\d{1,18}) triggered\Z"),
    re.compile(r"\Arender (\d{1,18}) cancelled\Z"),
    re.compile(r"\A(?:render|export) (\d{1,18}) deleted by "),
    re.compile(r"\Arender (\d{1,18}) failed: "),
    re.compile(r"\Aexport (\d{1,18}) produced a zip of "),
)
# Event types whose metadata ``render_id`` the application sets itself.
_JOB_EVENT_TYPES = ("render.failed", "export.complete")
# A remembered id further than this above the table's own max(id) is not
# believable (it would take a million deleted jobs) and is ignored.
_MAX_JUMP = 1_000_000


def _job_ids_in_log(bind: sa.engine.Connection) -> list[int]:
    """Job ids recorded in structured event metadata and fixed-form messages."""
    ids: list[int] = []
    rows = bind.execute(
        sa.text(
            "SELECT json_extract(metadata, '$.render_id') FROM event "
            "WHERE json_valid(metadata) "
            "AND json_extract(metadata, '$.type') IN (:failed, :exported)"
        ),
        {"failed": _JOB_EVENT_TYPES[0], "exported": _JOB_EVENT_TYPES[1]},
    ).scalars()
    ids += [value for value in rows if isinstance(value, int) and value > 0]
    messages = bind.execute(
        sa.text(
            "SELECT message FROM event WHERE scope = 'project' "
            "AND (message LIKE '%render %' OR message LIKE 'export %')"
        )
    ).scalars()
    for message in messages:
        for pattern in _JOB_MESSAGES:
            match = pattern.match(message or "")
            if match:
                ids.append(int(match.group(1)))
                break
    return ids


def _highest_known_job_id(bind: sa.engine.Connection) -> int:
    """The highest render-job id in the table or believably recorded in the log."""
    highest_row = bind.execute(sa.text("SELECT max(id) FROM render_job")).scalar() or 0
    ceiling = highest_row + _MAX_JUMP
    highest = highest_row
    for job_id in _job_ids_in_log(bind):
        if job_id > ceiling:
            logger.warning(
                "ignoring render job id %d named in the event log: more than %d "
                "above the highest existing id %d",
                job_id,
                _MAX_JUMP,
                highest_row,
            )
            continue
        highest = max(highest, job_id)
    return highest


def upgrade() -> None:
    with suspend_foreign_keys(op):
        with op.batch_alter_table(
            "render_job",
            recreate="always",
            table_kwargs={"sqlite_autoincrement": True},
        ):
            pass
        bind = op.get_bind()
        floor = _highest_known_job_id(bind)
        # The row copy already set the sequence to max(id); raise it to cover
        # ids the event log mentions. An empty table has no sequence row yet.
        updated = bind.execute(
            sa.text(
                "UPDATE sqlite_sequence SET seq = max(seq, :floor) "
                "WHERE name = 'render_job'"
            ),
            {"floor": floor},
        ).rowcount
        if not updated and floor:
            bind.execute(
                sa.text(
                    "INSERT INTO sqlite_sequence (name, seq) "
                    "VALUES ('render_job', :floor)"
                ),
                {"floor": floor},
            )


def downgrade() -> None:
    with suspend_foreign_keys(op):
        with op.batch_alter_table(
            "render_job",
            recreate="always",
            table_kwargs={"sqlite_autoincrement": False},
        ):
            pass
        op.execute("DELETE FROM sqlite_sequence WHERE name = 'render_job'")
