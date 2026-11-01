"""Web tests for the frame metadata views (F4, #315).

Covers:
- GET /projects/{pid}/frames/{fid}/drawer — the drawer fragment carries the scene
  metadata the removed Info panel showed (#315); the /metadata route is gone.
- GET /projects/{pid}/frames/{fid} — the full-page no-JS fallback:
  - Renders the frame_detail.html page with the same metadata partial.
  - 404 for a frame in a different project.

Any authenticated role can fetch metadata (CurrentUser, not OperatorUser).
Viewer access is verified.

Helpers are local; seed helpers write directly to the running app's session
factory via ``get_context()`` like the other web test files.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

_FULL_ENVELOPE: dict = {
    "schema_version": 1,
    "source": "vapix",
    "captured_resolution": "1920x1080",
    "appearance_resolution": "1280x720",
    "brightness": "50",
    "contrast": "55",
}


def _seed_project(*, name: str) -> tuple[int, int]:
    """Seed a Camera + Project; return (project_id, camera_id)."""
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
        return proj.id, cam.id


def _seed_frame(
    project_id: int,
    *,
    seq: int = 1,
    scene_metadata: dict | None = None,
) -> int:
    """Seed a captured Frame in the given project; return its id."""
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
            scene_metadata=scene_metadata,
        )
        db.add(frame)
        db.flush()
        return frame.id


# #315 — the drawer fragment carries the scene metadata (the removed Info panel)
# ---------------------------------------------------------------------------

_HX = {"HX-Request": "true"}


class TestFrameDrawerSceneMetadata:
    def test_full_envelope_renders_groups(self, admin_client: TestClient) -> None:
        """The drawer shows the scene groups and rows, open, with the schema version."""
        project_id, _ = _seed_project(name="Meta Full")
        frame_id = _seed_frame(project_id, seq=1, scene_metadata=_FULL_ENVELOPE)
        resp = admin_client.get(
            f"/projects/{project_id}/frames/{frame_id}/drawer", headers=_HX
        )
        assert resp.status_code == 200
        html = resp.text
        assert '<details class="frame-drawer-scene" open>' in html
        assert "Capture" in html
        assert "Appearance" in html
        assert "Resolution" in html
        assert "Source" in html
        assert "schema v1" in html

    def test_null_metadata_renders_collapsed_none(
        self, admin_client: TestClient
    ) -> None:
        """No scene metadata: the disclosure says (none) and stays collapsed."""
        project_id, _ = _seed_project(name="Meta Null")
        frame_id = _seed_frame(project_id, seq=1, scene_metadata=None)
        resp = admin_client.get(
            f"/projects/{project_id}/frames/{frame_id}/drawer", headers=_HX
        )
        assert resp.status_code == 200
        assert '<details class="frame-drawer-scene">' in resp.text
        assert "Scene metadata (none)" in resp.text

    def test_viewer_can_open_the_drawer(self, viewer_client: TestClient) -> None:
        """Viewing a frame is a read: any authenticated role."""
        project_id, _ = _seed_project(name="Meta Viewer")
        frame_id = _seed_frame(project_id, seq=1, scene_metadata=_FULL_ENVELOPE)
        resp = viewer_client.get(
            f"/projects/{project_id}/frames/{frame_id}/drawer", headers=_HX
        )
        assert resp.status_code == 200
        assert "Scene metadata" in resp.text

    def test_metadata_fragment_route_is_gone(self, admin_client: TestClient) -> None:
        """The Info button's /metadata fragment endpoint was removed with it."""
        project_id, _ = _seed_project(name="Meta Gone")
        frame_id = _seed_frame(project_id, seq=1, scene_metadata=_FULL_ENVELOPE)
        resp = admin_client.get(f"/projects/{project_id}/frames/{frame_id}/metadata")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# F4 — GET /projects/{pid}/frames/{fid} (full-page no-JS fallback)
# ---------------------------------------------------------------------------


class TestFrameDetailFallbackPage:
    def test_full_page_renders_frame_detail_template(
        self, admin_client: TestClient
    ) -> None:
        """The no-JS fallback renders as a full page with breadcrumb and metadata."""
        project_id, _ = _seed_project(name="Detail Full")
        frame_id = _seed_frame(project_id, seq=3, scene_metadata=_FULL_ENVELOPE)
        resp = admin_client.get(f"/projects/{project_id}/frames/{frame_id}")
        assert resp.status_code == 200
        html = resp.text
        # Full-page breadcrumb uses the frame sequence.
        assert "Frame #3" in html
        # Metadata partial is included.
        assert "Scene metadata" in html
        assert "Resolution" in html
        # "Back to frames" link is rendered.
        assert "Back to frames" in html
        # The panel-only close button must NOT appear on the standalone page:
        # it targets #frame-meta-panel, which does not exist here (would throw).
        assert "Close metadata panel" not in html

    def test_full_page_null_metadata_renders_placeholder(
        self, admin_client: TestClient
    ) -> None:
        """The no-JS fallback renders the null state when scene_metadata is absent."""
        project_id, _ = _seed_project(name="Detail Null")
        frame_id = _seed_frame(project_id, seq=1, scene_metadata=None)
        resp = admin_client.get(f"/projects/{project_id}/frames/{frame_id}")
        assert resp.status_code == 200
        assert "No scene metadata recorded for this frame" in resp.text
        assert "Frame #1" in resp.text

    def test_full_page_frame_in_different_project_returns_404(
        self, admin_client: TestClient
    ) -> None:
        """The no-JS fallback also returns 404 for a cross-project frame lookup."""
        project_a_id, _ = _seed_project(name="Detail IDOR A")
        project_b_id, _ = _seed_project(name="Detail IDOR B")
        frame_id = _seed_frame(project_b_id, seq=1)
        resp = admin_client.get(f"/projects/{project_a_id}/frames/{frame_id}")
        assert resp.status_code == 404

    def test_viewer_can_access_frame_detail_page(
        self, viewer_client: TestClient
    ) -> None:
        """The no-JS fallback is accessible to all authenticated roles."""
        project_id, _ = _seed_project(name="Detail Viewer")
        frame_id = _seed_frame(project_id, seq=2, scene_metadata=_FULL_ENVELOPE)
        resp = viewer_client.get(f"/projects/{project_id}/frames/{frame_id}")
        assert resp.status_code == 200
        assert "Frame #2" in resp.text
