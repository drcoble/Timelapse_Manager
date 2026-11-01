"""``get_active_alerts`` floors keep their meaning after the index rewrite (#66).

The alert reads now repeat the partial index's level list as literals (see
``monitoring.alerts._open_alert_criteria``). That must stay a pure planning
aid: the threshold, a higher floor, a lower floor and an unknown floor each
return exactly the rows they did before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from timelapse_manager.db.models import Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring.alerts import get_active_alerts


def _seed(factory: Any) -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    with session_scope(factory) as session:
        for minutes, level in enumerate(
            ["debug", "info", "warning", "error", "critical"]
        ):
            session.add(
                Event(
                    scope="system",
                    level=level,
                    message=f"open {level}",
                    timestamp=now - timedelta(minutes=minutes),
                )
            )
        session.add(
            Event(
                scope="system",
                level="error",
                message="cleared error",
                timestamp=now,
                alert_cleared_at=now,
                alert_clear_reason="manual",
            )
        )


@pytest.mark.parametrize(
    ("floor", "expected"),
    [
        (None, {"warning", "error", "critical"}),
        ("warning", {"warning", "error", "critical"}),
        ("error", {"error", "critical"}),
        ("critical", {"critical"}),
        ("info", {"info", "warning", "error", "critical"}),
        ("debug", {"debug", "info", "warning", "error", "critical"}),
        ("no-such-level", {"debug", "info", "warning", "error", "critical"}),
    ],
)
def test_floor_selects_the_same_rows(
    migrated_factory: Any, floor: str | None, expected: set[str]
) -> None:
    _seed(migrated_factory)

    with session_scope(migrated_factory) as session:
        rows, total = get_active_alerts(session, level_floor=floor)

    assert {row.level for row in rows} == expected
    assert total == len(expected)
    assert all(row.message.startswith("open") for row in rows)
