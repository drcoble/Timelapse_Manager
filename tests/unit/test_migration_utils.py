"""The ``suspend_foreign_keys`` guard must verify the pragma actually took hold.

These tests drive the guard with a minimal stand-in for Alembic's ``op`` that
wraps a raw ``sqlite3`` connection, exposing just the surface the guard uses:
``get_bind()`` (with a ``dialect.name`` and ``exec_driver_sql``) and
``execute()``. Running against a real connection lets us reproduce the no-op
case (pragma issued inside an open transaction) deterministically.
"""

from __future__ import annotations

import sqlite3

import pytest

from timelapse_manager.db.migration_utils import suspend_foreign_keys


class _FakeResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _FakeDialect:
    name = "sqlite"


class _FakeBind:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn
        self.dialect = _FakeDialect()

    def exec_driver_sql(self, statement: str) -> _FakeResult:
        row = self.conn.execute(statement).fetchone()
        return _FakeResult(row[0] if row is not None else None)


class _FakeOp:
    """Stand-in for ``alembic.operations.Operations`` over a raw connection."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._bind = _FakeBind(conn)

    def get_bind(self) -> _FakeBind:
        return self._bind

    def execute(self, statement: str) -> None:
        self._bind.conn.execute(statement)


def _autocommit_connection() -> sqlite3.Connection:
    """A connection in autocommit mode with FK enforcement on (no open txn)."""
    conn = sqlite3.connect(":memory:")
    conn.isolation_level = None  # We control transaction boundaries explicitly.
    conn.execute("PRAGMA foreign_keys=ON")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    return conn


def test_guard_disables_then_restores_enforcement() -> None:
    """With no transaction open, enforcement is off inside and back on after."""
    conn = _autocommit_connection()
    try:
        op = _FakeOp(conn)
        with suspend_foreign_keys(op):
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_guard_raises_when_pragma_is_a_no_op() -> None:
    """An open transaction makes the OFF pragma a no-op, which must be loud."""
    conn = _autocommit_connection()
    try:
        conn.execute("BEGIN")  # Open a transaction: PRAGMA foreign_keys is ignored.
        op = _FakeOp(conn)
        with (
            pytest.raises(RuntimeError, match="foreign-key enforcement"),
            suspend_foreign_keys(op),
        ):
            pytest.fail("guard should raise before yielding control")
    finally:
        conn.close()
