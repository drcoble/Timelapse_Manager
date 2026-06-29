"""Web tests for the frame metadata endpoints (F4).

Covers:
- GET /projects/{pid}/frames/{fid}/metadata — the HTMX fragment endpoint:
  - With a full scene-metadata envelope: groups and rows render.
  - Null/absent metadata: the "No scene metadata" placeholder renders.
  - Frame belongs to a different project: 404 (anti-IDOR guard).
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


# ---------------------------------------------------------------------------
# F4 — GET /projects/{pid}/frames/{fid}/metadata (HTMX fragment)
# ---------------------------------------------------------------------------


class TestFrameMetadataFragment:
    def test_full_envelope_renders_groups(self, admin_client: TestClient) -> None:
        """A frame with a full scene-metadata envelope renders its groups and rows."""
        project_id, _ = _seed_project(name="Meta Full")
        frame_id = _seed_frame(project_id, seq=1, scene_metadata=_FULL_ENVELOPE)
        resp = admin_client.get(f"/projects/{project_id}/frames/{frame_id}/metadata")
        assert resp.status_code == 200
        html = resp.text
        # Partial renders "Scene metadata — Frame #1".
        assert "Scene metadata" in html
        assert "Frame #1" in html
        # Groups: Capture, Appearance, Exposure.
        assert "Capture" in html
        assert "Appearance" in html
        # Row labels from the Capture group.
        assert "Resolution" in html
        assert "Source" in html
        # schema_version is rendered.
        assert "schema v1" in html
        # No "No scene metadata" placeholder.
        assert "No scene metadata recorded" not in html
        # In the HTMX panel the close button is present (it clears the panel).
        assert "Close metadata panel" in html

    def test_null_metadata_renders_placeholder(self, admin_client: TestClient) -> None:
        """A frame with no scene metadata renders the 'No scene metadata' notice."""
        project_id, _ = _seed_project(name="Meta Null")
        frame_id = _seed_frame(project_id, seq=1, scene_metadata=None)
        resp = admin_client.get(f"/projects/{project_id}/frames/{frame_id}/metadata")
        assert resp.status_code == 200
        html = resp.text
        assert "No scene metadata recorded for this frame" in html
        # No group titles should appear.
        assert "Capture" not in html

    def test_empty_envelope_renders_placeholder(self, admin_client: TestClient) -> None:
        """An empty dict scene_metadata renders the 'No scene metadata' notice."""
        project_id, _ = _seed_project(name="Meta Empty")
        frame_id = _seed_frame(project_id, seq=1, scene_metadata={})
        resp = admin_client.get(f"/projects/{project_id}/frames/{frame_id}/metadata")
        assert resp.status_code == 200
        assert "No scene metadata recorded for this frame" in resp.text

    def test_frame_in_different_project_returns_404(
        self, admin_client: TestClient
    ) -> None:
        """Requesting a frame from the wrong project returns 404 (anti-IDOR guard)."""
        project_a_id, _ = _seed_project(name="Meta IDOR A")
        project_b_id, _ = _seed_project(name="Meta IDOR B")
        # Frame lives in project B.
        frame_id = _seed_frame(project_b_id, seq=1)
        # Request it via project A's URL.
        resp = admin_client.get(f"/projects/{project_a_id}/frames/{frame_id}/metadata")
        assert resp.status_code == 404

    def test_nonexistent_frame_returns_404(self, admin_client: TestClient) -> None:
        """A frame id that does not exist returns 404."""
        project_id, _ = _seed_project(name="Meta Missing")
        resp = admin_client.get(f"/projects/{project_id}/frames/999999/metadata")
        assert resp.status_code == 404

    def test_viewer_can_access_metadata(self, viewer_client: TestClient) -> None:
        """Metadata endpoint is open to all authenticated roles (CurrentUser)."""
        project_id, _ = _seed_project(name="Meta Viewer")
        frame_id = _seed_frame(project_id, seq=1, scene_metadata=_FULL_ENVELOPE)
        resp = viewer_client.get(f"/projects/{project_id}/frames/{frame_id}/metadata")
        assert resp.status_code == 200
        assert "Scene metadata" in resp.text


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
