"""Web tests for the frame-detail drawer.

Covers:
- GET /projects/{pid}/frames/{fid}/drawer
  - With HX-Request: returns the drawer-body fragment (drawer title, image URL,
    prev/next controls).
  - Without HX-Request: returns the full-page no-JS fallback.
  - A frame in a different project returns 404 (anti-IDOR guard).
  - Newer/older neighbour ids are correct, including at the series ends.
- PATCH /projects/{pid}/frames/{fid}?drawer=1 returns the updated timestamp row.
- POST .../soft-delete?drawer=1 returns a drawer body carrying an out-of-band
  tile update.

Seed helpers write directly to the running app's session factory via
``get_context()`` like the other web test files.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_HX = {"HX-Request": "true"}
_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _seed_project(*, name: str) -> int:
    """Seed a Camera + Project; return the project id."""
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


def _seed_frame(
    project_id: int,
    *,
    seq: int,
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


class TestDrawerFragment:
    def test_hx_request_returns_drawer_fragment(self, admin_client: TestClient) -> None:
        """An HX-Request gets the drawer-body fragment, not the full page."""
        pid = _seed_project(name="Drawer Frag")
        fid = _seed_frame(pid, seq=5)
        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/drawer", headers=_HX)
        assert resp.status_code == 200
        html = resp.text
        # Drawer title attribute drives the header text.
        assert 'data-drawer-title="Frame #5"' in html
        # The full-width image references the image route.
        assert f"/projects/{pid}/frames/{fid}/image" in html
        # The no-JS fallback page chrome must NOT be present in the fragment.
        assert "<!DOCTYPE html>" not in html
        # Detail fields.
        assert "1920" in html
        assert "Captured" in html

    def test_no_hx_request_returns_full_page(self, admin_client: TestClient) -> None:
        """A plain (no-JS) request gets the full-page detail fallback."""
        pid = _seed_project(name="Drawer Page")
        fid = _seed_frame(pid, seq=2)
        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/drawer")
        assert resp.status_code == 200
        html = resp.text
        assert "<!DOCTYPE html>" in html
        assert "Frame #2" in html

    def test_prev_next_controls_present(self, admin_client: TestClient) -> None:
        """A middle frame's drawer renders links to both neighbours."""
        pid = _seed_project(name="Drawer Nav")
        f1 = _seed_frame(pid, seq=1)
        f2 = _seed_frame(pid, seq=2)
        f3 = _seed_frame(pid, seq=3)
        resp = admin_client.get(f"/projects/{pid}/frames/{f2}/drawer", headers=_HX)
        html = resp.text
        # Newer neighbour is the higher sequence index (f3); older is f1.
        assert f"/projects/{pid}/frames/{f3}/drawer" in html
        assert f"/projects/{pid}/frames/{f1}/drawer" in html
        assert "Newer" in html
        assert "Older" in html

    def test_neighbor_ids_at_series_ends(self, admin_client: TestClient) -> None:
        """The newest frame has no newer link; the oldest has no older link."""
        pid = _seed_project(name="Drawer Ends")
        oldest = _seed_frame(pid, seq=1)
        newest = _seed_frame(pid, seq=2)

        newest_html = admin_client.get(
            f"/projects/{pid}/frames/{newest}/drawer", headers=_HX
        ).text
        # No drawer link to a newer frame (it is the newest).
        assert 'rel="prev"' not in newest_html
        # But it does link to the older one.
        assert f"/projects/{pid}/frames/{oldest}/drawer" in newest_html

        oldest_html = admin_client.get(
            f"/projects/{pid}/frames/{oldest}/drawer", headers=_HX
        ).text
        assert 'rel="next"' not in oldest_html
        assert f"/projects/{pid}/frames/{newest}/drawer" in oldest_html

    def test_frame_in_different_project_returns_404(
        self, admin_client: TestClient
    ) -> None:
        """A cross-project frame lookup is a 404 (anti-IDOR guard)."""
        pa = _seed_project(name="Drawer IDOR A")
        pb = _seed_project(name="Drawer IDOR B")
        fid = _seed_frame(pb, seq=1)
        resp = admin_client.get(f"/projects/{pa}/frames/{fid}/drawer", headers=_HX)
        assert resp.status_code == 404

    def test_bare_detail_route_still_works(self, admin_client: TestClient) -> None:
        """The /drawer suffix does not shadow the bare detail route."""
        pid = _seed_project(name="Drawer Coexist")
        fid = _seed_frame(pid, seq=1)
        assert admin_client.get(f"/projects/{pid}/frames/{fid}").status_code == 200
        assert (
            admin_client.get(f"/projects/{pid}/frames/{fid}/drawer").status_code == 200
        )


class TestDrawerTimestampEdit:
    def test_patch_drawer_returns_updated_row(self, admin_client: TestClient) -> None:
        """PATCH ...?drawer=1 returns the timestamp row reflecting the new value."""
        pid = _seed_project(name="Drawer TS")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(admin_client, "/frames")
        resp = admin_client.patch(
            f"/projects/{pid}/frames/{fid}?drawer=1",
            data={"capture_timestamp": "2026-03-04T09:30", "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        html = resp.text
        # The response is the timestamp row partial, in display mode.
        assert 'id="frame-timestamp-row"' in html
        # New value pre-fills the (hidden) edit input.
        assert "2026-03-04T09:30" in html


class TestDrawerSoftDelete:
    def test_soft_delete_drawer_returns_body_with_oob_tile(
        self, admin_client: TestClient
    ) -> None:
        """Soft-delete ...?drawer=1 returns the drawer body for the same frame in
        its deleted state, plus an out-of-band tile re-render."""
        pid = _seed_project(name="Drawer Del")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(admin_client, "/frames")
        resp = admin_client.post(
            f"/projects/{pid}/frames/{fid}/soft-delete?drawer=1",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        html = resp.text
        # Drawer body for the same frame (still its title).
        assert 'data-drawer-title="Frame #1"' in html
        # Out-of-band tile update for the underlying grid tile.
        assert "hx-swap-oob" in html
        assert f'id="frame-tile-{fid}"' in html
        # In the deleted state, the drawer footer offers Restore.
        assert "Restore" in html

    def test_restore_drawer_returns_body_with_oob_tile(
        self, admin_client: TestClient
    ) -> None:
        """Restore ...?drawer=1 returns the drawer body + an OOB tile update."""
        pid = _seed_project(name="Drawer Restore")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(admin_client, "/frames")
        # Delete first so restore has something to do.
        admin_client.post(
            f"/projects/{pid}/frames/{fid}/soft-delete?drawer=1",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        resp = admin_client.post(
            f"/projects/{pid}/frames/{fid}/restore?drawer=1",
            data={"csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        html = resp.text
        assert 'data-drawer-title="Frame #1"' in html
        assert "hx-swap-oob" in html
        # Back in the active state, the footer offers Remove (soft-delete).
        assert "Remove" in html
