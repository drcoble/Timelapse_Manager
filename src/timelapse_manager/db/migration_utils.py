"""Helpers shared by Alembic migrations.

The one helper here, :func:`suspend_foreign_keys`, makes SQLite table rebuilds
safe. SQLite cannot ``ALTER`` most column or constraint changes in place, so
Alembic performs them in "batch" mode: it creates a new table, copies the rows
across, drops the original, and renames the copy. That implicit ``DROP TABLE``
fires the ``ON DELETE CASCADE`` foreign keys declared on child tables, silently
deleting their rows even though the parent's data is preserved by the copy.

Wrapping the batch block in this context manager turns foreign-key enforcement
off for the duration of the rebuild, so the cascade does not run and child rows
survive. This relies on Alembic applying SQLite DDL outside a transaction here
(``PRAGMA foreign_keys`` is silently ignored mid-transaction); the project's
migration environment runs in that non-transactional mode. On any non-SQLite
dialect the context manager is a no-op.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.operations import Operations


@contextmanager
def suspend_foreign_keys(op: Operations) -> Iterator[None]:
    """Disable SQLite foreign-key enforcement for the duration of the block.

    Use this around an :func:`~alembic.op.batch_alter_table` block whose rebuild
    would otherwise cascade-delete child rows through ``ON DELETE CASCADE``. The
    pragma is restored in a ``finally`` so enforcement is re-enabled even if the
    wrapped DDL raises. On non-SQLite dialects this does nothing.

    Important: the ``OFF`` pragma must be the first statement issued in the
    migration step. SQLite silently ignores ``PRAGMA foreign_keys`` while a
    transaction is open, so any DML (e.g. a fold-back ``UPDATE``) run *before*
    this context manager opens a transaction that makes the pragma a no-op and
    the cascade fires anyway. Put such DML *inside* the ``with`` block, after
    the guard has turned enforcement off. The guard only works because the
    project's Alembic environment applies SQLite DDL non-transactionally, which
    leaves no transaction open when the pragma is issued.

    To keep that invariant from failing silently, the pragma value is read back
    immediately after it is set: if enforcement is still on (typically because a
    transaction was already open before the guard ran) a :class:`RuntimeError`
    is raised so the misuse surfaces loudly instead of letting the rebuild
    cascade-delete child rows.
    """
    bind = op.get_bind()
    is_sqlite = bind.dialect.name == "sqlite"
    if is_sqlite:
        op.execute("PRAGMA foreign_keys=OFF")
        # Reading the pragma reflects the connection's current state regardless
        # of any open transaction, so this reliably detects the no-op case.
        still_enforced = bind.exec_driver_sql("PRAGMA foreign_keys").scalar()
        if still_enforced:
            raise RuntimeError(
                "Could not suspend SQLite foreign-key enforcement: "
                "'PRAGMA foreign_keys=OFF' was a no-op. SQLite ignores this "
                "pragma while a transaction is open, so a statement most likely "
                "ran before this guard. Make 'suspend_foreign_keys' the first "
                "thing the migration step does and move any preceding DML inside "
                "the 'with' block."
            )
    try:
        yield
    finally:
        if is_sqlite:
            op.execute("PRAGMA foreign_keys=ON")
