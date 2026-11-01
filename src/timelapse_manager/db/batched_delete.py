"""Delete rows in short, bounded transactions (issue #66, C4).

Retention jobs delete potentially many rows from tables the app writes to all
the time. One big ``DELETE`` would hold SQLite's single write lock for the
whole statement and stall capture and requests behind it. :func:`prune_in_batches`
instead deletes at most ``batch_size`` rows per transaction, commits, and stops
after ``max_batches`` transactions, so each lock hold is short and one pass has
a fixed ceiling. Rows left over are picked up by the next pass.

The statement builder owns the row selection. The usual shape bounds the batch
with a sub-select, because SQLite's ``DELETE ... LIMIT`` is a compile-time
option that is usually off, so it cannot be relied on::

    def build(limit: int) -> Delete:
        ids = select(Model.id).where(<predicate>).order_by(Model.id).limit(limit)
        return delete(Model).where(Model.id.in_(ids))
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

from sqlalchemy import CursorResult, Executable
from sqlalchemy.orm import Session, sessionmaker

from .session import session_scope

logger = logging.getLogger(__name__)


def prune_in_batches(
    session_factory: sessionmaker[Session],
    stmt_builder: Callable[[int], Executable],
    batch_size: int,
    max_batches: int,
) -> int:
    """Run ``stmt_builder(batch_size)`` repeatedly, one transaction per batch.

    Each batch runs in its own :func:`session_scope`, so it commits (and
    releases the write lock) before the next one starts. The loop stops when a
    batch deletes fewer than ``batch_size`` rows (nothing more to do) or after
    ``max_batches`` batches (the per-pass cap). Synchronous: call it off the
    event loop.

    :param session_factory: factory for the short per-batch sessions.
    :param stmt_builder: returns the DML statement for one batch, given the
        batch size. It must delete at most that many rows, and must be safe to
        run again (each call selects the rows that still match).
    :param batch_size: rows per batch; must be at least 1.
    :param max_batches: batches per call; must be at least 1.
    :returns: the total number of rows deleted.
    :raises ValueError: if ``batch_size`` or ``max_batches`` is below 1.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be at least 1, got {batch_size}")
    if max_batches < 1:
        raise ValueError(f"max_batches must be at least 1, got {max_batches}")
    total = 0
    for batch in range(1, max_batches + 1):
        with session_scope(session_factory) as session:
            result = cast(
                "CursorResult[Any]", session.execute(stmt_builder(batch_size))
            )
            deleted = int(result.rowcount or 0)
        total += deleted
        if deleted < batch_size:
            return total
        if batch == max_batches:
            logger.info(
                "batched delete stopped at the %d-batch cap after %d rows; "
                "the next pass continues",
                max_batches,
                total,
            )
    return total
