"""Capture-health surfacing: the overdue (silent-stall) signal and the
error-with-last-message banner on the dashboard card + project detail."""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.routers._viewmodels import _capture_is_overdue

_UTC = datetime.UTC


def _ago(seconds: int) -> datetime.datetime:
    return datetime.datetime.now(_UTC).replace(tzinfo=None) - datetime.timedelta(
        seconds=seconds
    )


@dataclass
class _FakeState:
    """A live CaptureState stand-in carrying just the fields the view reads."""

    state: str
    last_capture_at: datetime.datetime | None = None
    last_error: str | None = None
    last_error_at: datetime.datetime | None = None
    started_at: datetime.datetime | None = None
    pause_reason: str | None = None


def _seed_project(*, frame_count: int = 50, interval: int = 60) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="health-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name="health-project",
            capture_interval_seconds=interval,
            lifecycle_state="active",
            operational_status="idle",
            storage_path="/tmp/health-project",
            frame_count=frame_count,
        )
        db.add(proj)
        db.flush()
        return proj.id


def _seed_captured_frame(project_id: int, *, seconds_ago: int) -> None:
    """Record a frame the app captured ``seconds_ago`` (Last Capture reads the DB)."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        db.add(
            Frame(
                project_id=project_id,
                sequence_index=1,
                capture_timestamp=_ago(seconds_ago),
                file_path="f1.jpg",
                origin="captured",
                lifecycle_state="active",
            )
        )


# --- pure logic -------------------------------------------------------------


class TestOverdueLogic:
    def test_running_with_stale_capture_is_overdue(self) -> None:
        assert _capture_is_overdue("running", 10, _ago(600), 60) is True

    def test_running_with_recent_capture_is_not_overdue(self) -> None:
        assert _capture_is_overdue("running", 10, _ago(30), 60) is False

    def test_not_running_is_never_overdue(self) -> None:
        assert _capture_is_overdue("paused", 10, _ago(99999), 60) is False

    def test_no_frames_is_not_overdue(self) -> None:
        # A running project that has never captured is "starting", not stalled.
        assert _capture_is_overdue("running", 0, _ago(600), 60) is False

    def test_missing_inputs_are_not_overdue(self) -> None:
        assert _capture_is_overdue("running", 10, None, 60) is False
        assert _capture_is_overdue("running", 10, _ago(600), None) is False


# --- surfacing --------------------------------------------------------------


def _with_state(fake: _FakeState):
    ctx = get_context()
    previous = ctx.capture_supervisor
    sup = MagicMock()
    sup.state_for_project.return_value = fake
    ctx.capture_supervisor = sup
    return previous


def test_error_state_shows_banner_with_message(operator_client: TestClient) -> None:
    pid = _seed_project()
    previous = _with_state(
        _FakeState(
            state="error", last_error="Camera unreachable", last_error_at=_ago(30)
        )
    )
    try:
        html = operator_client.get(f"/projects/{pid}").text
        assert "Capture error" in html
        assert "Camera unreachable" in html
        assert "View events" in html
    finally:
        get_context().capture_supervisor = previous


def test_overdue_running_shows_banner_and_card_badge(
    operator_client: TestClient,
) -> None:
    pid = _seed_project(interval=60)
    # Since #5 the last capture is read from the frame table, not runner memory.
    _seed_captured_frame(pid, seconds_ago=600)
    previous = _with_state(_FakeState(state="running"))
    try:
        detail = operator_client.get(f"/projects/{pid}").text
        assert "Capture overdue" in detail
        dashboard = operator_client.get("/").text
        assert "Overdue" in dashboard
    finally:
        get_context().capture_supervisor = previous


def _info_value(html: str, label: str) -> str:
    """Return the text of the ``info-value`` cell following ``label``."""
    import re

    match = re.search(
        rf'<dt class="info-label">{label}</dt>\s*<dd class="info-value">(.*?)</dd>',
        html,
        re.S,
    )
    assert match is not None, f"{label} cell not found"
    return re.sub(r"<[^>]+>", "", match.group(1)).strip()


def test_anchor_only_project_shows_last_and_next_capture(
    operator_client: TestClient,
) -> None:
    """#5: an anchor-only project read '—' for both, even while capturing."""
    pid = _seed_project(interval=60)
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = db.get(Project, pid)
        assert proj is not None
        proj.capture_interval_seconds = None
        proj.schedule = {"enabled": True, "timezone": "America/New_York"}
        proj.exact_time_anchors = [
            {"id": "noon", "kind": "clock", "time": "12:00", "enabled": True}
        ]
    _seed_captured_frame(pid, seconds_ago=3600)
    previous = _with_state(_FakeState(state="running"))  # no in-memory capture
    try:
        detail = operator_client.get(f"/projects/{pid}").text
        assert _info_value(detail, "Last Capture") not in ("", "—", "&mdash;")
        assert _info_value(detail, "Next Capture") not in ("", "—", "&mdash;")
    finally:
        get_context().capture_supervisor = previous
