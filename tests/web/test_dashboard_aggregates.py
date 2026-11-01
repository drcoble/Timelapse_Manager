"""Dashboard frame aggregates in one batched read (#66 C5).

* The project view models built from the batched read equal the values the old
  per-project helpers give, field by field (last capture, preview URL, disk
  usage, storage projection and growth rate), and the list builder agrees with
  the single-project path.
* ``/partials/projects`` renders the same HTML whether the frame figures come
  from the batched read or from the old per-project helpers.
* The frame aggregates run once per request, not once per project. Only that is
  asserted: other per-project reads may remain, so the total statement count is
  not pinned.
* The partial keeps only active projects, now filtered in SQL.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event, insert

from timelapse_manager.capture import next_due
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.storage import estimator
from timelapse_manager.storage import frames as frame_service
from timelapse_manager.storage.frames import FrameAggregates
from timelapse_manager.web.routers._viewmodels import _project_view, _project_views

NOON_ANCHOR = [{"id": "noon", "kind": "clock", "time": "12:00", "enabled": True}]


@dataclass
class _RunningState:
    """A live runner stand-in, so growth rates are computed (#22 D12)."""

    state: str = "running"
    pause_reason: str | None = None
    last_capture_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    started_at: datetime | None = None


@pytest.fixture
def running_supervisor(admin_client: TestClient) -> Iterator[None]:
    ctx = get_context()
    previous = ctx.capture_supervisor
    supervisor = MagicMock()
    supervisor.state_for_project.return_value = _RunningState()
    ctx.capture_supervisor = supervisor
    try:
        yield
    finally:
        ctx.capture_supervisor = previous


def _legacy_aggregates(session: Any, project_ids: Any) -> dict[int, FrameAggregates]:
    """The per-project reads the view made before #66 C5, in the new shape."""
    result = {}
    for project_id in project_ids:
        latest = frame_service.latest_active_frame(session, project_id)
        result[project_id] = FrameAggregates(
            last_captured_at=frame_service.latest_captured_at(session, project_id),
            disk_usage_bytes=frame_service.sum_project_disk_usage(session, project_id),
            latest_frame_id=latest.id if latest is not None else None,
        )
    return result


def _seed() -> dict[str, int]:
    """Projects covering the awkward cases, on two shared cameras."""
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    base = now - timedelta(days=20)
    with session_scope(get_context().session_factory) as db:
        cameras = [Camera(name=f"cam{n}") for n in range(2)]
        db.add_all(cameras)
        db.flush()
        specs: dict[str, dict[str, Any]] = {
            "mixed": {"end_date": now + timedelta(days=30)},
            "empty": {"end_date": now + timedelta(days=10)},
            "only_uploads": {},
            "late_import": {"end_date": now + timedelta(days=60)},
            "paused": {"lifecycle_state": "paused"},
        }
        projects = {}
        for n, (name, extra) in enumerate(specs.items()):
            project = Project(
                camera_id=cameras[n % 2].id,
                name=name,
                capture_interval_seconds=None,
                exact_time_anchors=NOON_ANCHOR,
                **extra,
            )
            db.add(project)
            projects[name] = project
        db.flush()
        ids = {name: project.id for name, project in projects.items()}

        def frame(pid: int, seq: int, **overrides: Any) -> dict[str, Any]:
            row: dict[str, Any] = {
                "project_id": pid,
                "sequence_index": seq,
                "capture_timestamp": base + timedelta(hours=seq),
                "file_path": f"{seq:08d}.jpg",
                "file_size_bytes": 200_000 + seq,
                "capture_status": "captured",
                "origin": "captured",
                "lifecycle_state": "active",
            }
            row.update(overrides)
            return row

        rows = [
            frame(
                ids["mixed"],
                seq,
                origin="uploaded" if seq % 4 == 0 else "captured",
                lifecycle_state="soft_deleted" if seq % 5 == 0 else "active",
                file_size_bytes=None if seq % 3 == 0 else 200_000 + seq,
            )
            for seq in range(1, 31)
        ]
        rows += [frame(ids["only_uploads"], s, origin="uploaded") for s in (1, 2)]
        rows += [frame(ids["late_import"], s) for s in range(10, 14)]
        rows += [frame(ids["paused"], s) for s in (1, 2)]
        db.execute(insert(Frame), rows)
        db.execute(
            insert(Frame),
            [
                frame(
                    ids["late_import"],
                    s,
                    origin="uploaded",
                    capture_timestamp=base - timedelta(days=400 - s),
                )
                for s in range(1, 10)
            ],
        )
        for name, project in projects.items():
            project.frame_count = sum(
                1
                for row in rows
                if row["project_id"] == ids[name] and row["lifecycle_state"] == "active"
            ) + (9 if name == "late_import" else 0)
    return ids


@pytest.mark.usefixtures("running_supervisor")
class TestViewEquivalence:
    def test_every_frame_field_matches_the_old_helpers(self) -> None:
        ids = _seed()

        with session_scope(get_context().session_factory) as db:
            views = {view.id: view for view in _project_views(db)}
            assert set(views) == set(ids.values())
            for project_id, view in views.items():
                project = db.get(Project, project_id)
                assert project is not None
                last = next_due.as_utc(frame_service.latest_captured_at(db, project_id))
                latest = frame_service.latest_active_frame(db, project_id)
                assert view.last_capture_at_raw == last
                assert view.latest_frame_url == (
                    f"/projects/{project_id}/frames/{latest.id}/thumbnail?size=640"
                    if latest is not None
                    else None
                )
                assert view.disk_used_bytes == frame_service.sum_project_disk_usage(
                    db, project_id
                )
                projected, remaining = estimator.estimate_for_project(db, project)
                assert view.projected_total_bytes == projected
                assert view.projected_frames_remaining == remaining
                rate = (
                    estimator.estimate_growth_rate_bytes_per_day(db, project)
                    if view.capture_planned and project.frame_count
                    else None
                )
                assert (view.growth_rate_display is None) == (rate is None)
                if rate is not None:
                    assert view.growth_rate_display is not None
                    assert view.growth_rate_display.startswith(f"{_fmt(rate)} / day")

        # The cases the seed exists for actually produced values.
        mixed = views[ids["mixed"]]
        assert mixed.projected_total_bytes is not None
        assert mixed.growth_rate_display is not None
        assert views[ids["empty"]].latest_frame_url is None
        assert views[ids["only_uploads"]].last_capture_at_raw is None
        assert views[ids["only_uploads"]].disk_used_bytes > 0

    def test_list_builder_matches_the_single_project_path(self) -> None:
        ids = _seed()

        with session_scope(get_context().session_factory) as db:
            batched = {view.id: view for view in _project_views(db)}
            for project_id in ids.values():
                project = db.get(Project, project_id)
                assert project is not None
                single = _project_view(db, project)
                assert _stable(single) == _stable(batched[project_id])

    def test_lifecycle_filter(self) -> None:
        ids = _seed()

        with session_scope(get_context().session_factory) as db:
            active = [view.id for view in _project_views(db, lifecycle_state="active")]
            paused = [view.id for view in _project_views(db, lifecycle_state="paused")]

        assert ids["paused"] not in active
        assert active == sorted(active)
        assert paused == [ids["paused"]]


def _fmt(num: int) -> str:
    from timelapse_manager.web.routers._viewmodels import _fmt_bytes

    return _fmt_bytes(num)


def _stable(view: Any) -> dict[str, Any]:
    """The view's fields minus the ones that depend on the wall clock."""
    fields = dict(vars(view))
    for name in ("next_capture_at_raw", "next_capture_at", "uptime_seconds"):
        fields.pop(name, None)
    return fields


@contextmanager
def _statements() -> Iterator[list[str]]:
    factory = get_context().session_factory
    engine = factory.kw["bind"]
    assert isinstance(engine, Engine)
    seen: list[str] = []

    def listener(*args: Any) -> None:
        seen.append(" ".join(str(args[2]).split()))

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", listener)


_CSRF_VALUE = re.compile(r'(name="csrf_token" value=")[^"]*(")')


def _without_csrf(html: str) -> str:
    """Blank the per-response masked CSRF token values in ``html``."""
    return _CSRF_VALUE.sub(r"\1\2", html)


_SUM_SIZES = re.compile(r"sum\(frame\.file_size_bytes\)", re.IGNORECASE)
_MAX_TIMES = re.compile(r"max\((CASE .*?)?frame\.capture_timestamp", re.IGNORECASE)
_LATEST_FRAME = re.compile(
    r"FROM frame WHERE .*ORDER BY frame\.sequence_index DESC", re.IGNORECASE
)


@pytest.mark.usefixtures("running_supervisor")
class TestProjectsPartial:
    def test_html_is_unchanged(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed()

        batched = admin_client.get("/partials/projects")
        with monkeypatch.context() as patch:
            patch.setattr(frame_service, "project_frame_aggregates", _legacy_aggregates)
            legacy = admin_client.get("/partials/projects")

        assert batched.status_code == legacy.status_code == 200
        # CSRF tokens are masked afresh per response (#70 F-S4); compare the rest.
        assert _without_csrf(batched.text) == _without_csrf(legacy.text)
        assert batched.text.count('<article class="project-card') == 4

    def test_frame_aggregates_run_once_not_per_project(
        self, admin_client: TestClient
    ) -> None:
        _seed()

        with _statements() as seen:
            resp = admin_client.get("/partials/projects")

        assert resp.status_code == 200
        assert len([s for s in seen if _SUM_SIZES.search(s)]) == 1, seen
        assert len([s for s in seen if _MAX_TIMES.search(s)]) == 1, seen
        # The preview lookup is one statement with a correlated subquery.
        assert len([s for s in seen if _LATEST_FRAME.search(s)]) == 1, seen

    def test_legacy_path_was_per_project(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Teeth: the counter does see the old per-project reads."""
        _seed()
        monkeypatch.setattr(
            frame_service, "project_frame_aggregates", _legacy_aggregates
        )

        with _statements() as seen:
            admin_client.get("/partials/projects")

        assert len([s for s in seen if _SUM_SIZES.search(s)]) == 4, seen

    def test_paused_projects_are_not_read(self, admin_client: TestClient) -> None:
        ids = _seed()

        with _statements() as seen:
            resp = admin_client.get("/partials/projects")

        assert f'id="project-card-{ids["paused"]}"' not in resp.text
        assert resp.text.count('<article class="project-card') == 4
        aggregate = next(s for s in seen if _SUM_SIZES.search(s))
        # Only the four active projects' ids reach the aggregate.
        assert "frame.project_id IN (?, ?, ?, ?)" in aggregate, aggregate
