"""Web tests for the single-frame exclude/include routes.

Covers:
- POST .../frames/{fid}/exclude and .../include:
  - operator allowed (200); viewer forbidden (403); CSRF required.
  - tile response (no ?drawer) vs drawer-body response (?drawer=1) shape.
  - the mutation records an audit Event.

Seed helpers write directly to the running app's session factory via
``get_context()`` like the other web test files.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_HX = {"HX-Request": "true"}
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _seed_project(*, name: str) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=f"{name}-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
        )
        db.add(proj)
        db.flush()
        return proj.id


def _seed_frame(project_id: int, *, seq: int) -> int:
    ctx = get_context()
    ts = datetime(2026, 1, 1, 0, seq, tzinfo=UTC).replace(tzinfo=None)
    with session_scope(ctx.session_factory) as db:
        frame = Frame(
            project_id=project_id,
            sequence_index=seq,
            capture_timestamp=ts,
            file_path=f"/frames/{project_id}/{seq:08d}.jpg",
            width=1920,
            height=1080,
            file_size_bytes=100_000,
            capture_status="captured",
            origin="captured",
            lifecycle_state="active",
        )
        db.add(frame)
        db.flush()
        return frame.id


def _excluded_at(frame_id: int):
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        frame = db.get(Frame, frame_id)
        assert frame is not None
        return frame.excluded_at


class TestExcludeRouteRoleGating:
    def test_operator_can_exclude(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exc Op")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            f"/projects/{pid}/frames/{fid}/exclude",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        assert _excluded_at(fid) is not None

    def test_viewer_forbidden(self, viewer_client: TestClient) -> None:
        """A viewer is 403 even with a valid CSRF token (the role gate, not CSRF)."""
        # Seed as the operator surface is unavailable to a viewer; seed directly.
        pid = _seed_project(name="Exc Viewer")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(viewer_client, "/frames")
        resp = viewer_client.post(
            f"/projects/{pid}/frames/{fid}/exclude",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 403
        assert _excluded_at(fid) is None

    def test_missing_csrf_rejected(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exc CSRF")
        fid = _seed_frame(pid, seq=1)
        resp = operator_client.post(
            f"/projects/{pid}/frames/{fid}/exclude",
            data={},
            headers=_FORM,
        )
        assert resp.status_code == 403
        assert _excluded_at(fid) is None


class TestExcludeIncludeResponseShape:
    def test_exclude_tile_response(self, operator_client: TestClient) -> None:
        """No ?drawer -> the single-frame tile fragment, not the drawer body."""
        pid = _seed_project(name="Exc Tile")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            f"/projects/{pid}/frames/{fid}/exclude",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        html = resp.text
        assert f'id="frame-tile-{fid}"' in html
        # A bare tile fragment carries no out-of-band swap marker (that only
        # appears when the tile rides along with a drawer-body mutation).
        assert "hx-swap-oob" not in html

    def test_exclude_drawer_response_with_oob_tile(
        self, operator_client: TestClient
    ) -> None:
        """?drawer=1 -> the drawer body for the same frame + an OOB tile update."""
        pid = _seed_project(name="Exc Drawer")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            f"/projects/{pid}/frames/{fid}/exclude?drawer=1",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        html = resp.text
        assert 'data-drawer-title="Frame #1"' in html
        assert "hx-swap-oob" in html
        assert f'id="frame-tile-{fid}"' in html
        # The re-rendered drawer reflects the new excluded state: the badge shows
        # and the footer offers Include (not Exclude).
        assert "badge-excluded" in html
        assert "Include in render" in html

    def test_include_clears_exclusion(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Inc Clear")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        operator_client.post(
            f"/projects/{pid}/frames/{fid}/exclude",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        assert _excluded_at(fid) is not None
        resp = operator_client.post(
            f"/projects/{pid}/frames/{fid}/include",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        assert _excluded_at(fid) is None


class TestExcludeRouteAudit:
    def test_exclude_records_event(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exc Audit")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        operator_client.post(
            f"/projects/{pid}/frames/{fid}/exclude",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            events = (
                db.query(Event)
                .filter(Event.scope == "project")
                .filter(Event.scope_id == pid)
                .all()
            )
        assert any(e.event_metadata.get("action") == "exclude" for e in events)

    def test_cross_project_frame_404(self, operator_client: TestClient) -> None:
        pa = _seed_project(name="Exc IDOR A")
        pb = _seed_project(name="Exc IDOR B")
        fid = _seed_frame(pb, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            f"/projects/{pa}/frames/{fid}/exclude",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 404
