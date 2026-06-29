"""Recent-activity mini-log + fps explainer on the project page, the relative-
time filter, and the project-scoped events keyset filter."""

from __future__ import annotations

import datetime

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.dependencies import _reltime_filter
from timelapse_manager.web.routers.events import _operational_events_keyset


def _seed_project_with_activity() -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="act-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name="act-project",
            capture_interval_seconds=300,
            lifecycle_state="active",
            operational_status="idle",
            storage_path="/tmp/act-project",
            frame_count=120,  # the stored counter drives the fps explainer
        )
        db.add(proj)
        db.flush()
        pid = proj.id
        db.add(
            Event(
                level="info",
                message="capture started here",
                scope="project",
                scope_id=pid,
            )
        )
        db.add(
            Frame(
                project_id=pid,
                sequence_index=1,
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
                file_path="/tmp/x/1.jpg",
            )
        )
        db.flush()
        return pid


def test_reltime_filter_buckets() -> None:
    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    assert _reltime_filter(now) == "just now"
    assert _reltime_filter(now - datetime.timedelta(minutes=5)) == "5m ago"
    assert _reltime_filter(now - datetime.timedelta(hours=3)) == "3h ago"
    assert _reltime_filter(now - datetime.timedelta(days=2)) == "2d ago"
    assert _reltime_filter(None) == ""


def test_events_keyset_scope_id_filters_to_one_project(
    operator_client: TestClient,  # installs the app context for get_context()
) -> None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        for pid in (101, 202):
            db.add(
                Event(level="info", message=f"p{pid}", scope="project", scope_id=pid)
            )
        db.flush()
        only = _operational_events_keyset(
            db,
            before_id=None,
            levels=[],
            q=None,
            scope="project",
            scope_id=101,
            limit=50,
        )
        assert {e.scope_id for e in only} == {101}
        # Without scope_id the helper is unfiltered by subject (both appear).
        both = _operational_events_keyset(
            db, before_id=None, levels=[], q=None, scope="project", limit=50
        )
        assert {101, 202} <= {e.scope_id for e in both}


def test_detail_shows_recent_activity_minilog(operator_client: TestClient) -> None:
    pid = _seed_project_with_activity()
    html = operator_client.get(f"/projects/{pid}").text
    assert "Recent Activity" in html
    assert "capture started here" in html


def test_detail_shows_fps_explainer(operator_client: TestClient) -> None:
    pid = _seed_project_with_activity()
    html = operator_client.get(f"/projects/{pid}").text
    # The fps explainer relates capture span to playback length.
    assert "fps," in html and "play in" in html
