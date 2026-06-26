"""Web tests for the keyset /frames/batch endpoint + continuous-scroll grid."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed(n_frames: int) -> tuple[int, dict[int, int]]:
    """Seed a project with n frames seq 0..n-1; return (project_id, {seq: frame_id})."""
    ctx = get_context()
    seq_to_id: dict[int, int] = {}
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="fk-cam", address="10.0.0.6", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(camera_id=cam.id, name="FK Project", lifecycle_state="active")
        db.add(proj)
        db.flush()
        pid = proj.id
        for i in range(n_frames):
            fr = Frame(
                project_id=pid,
                sequence_index=i,
                capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
                file_path=f"/frames/{pid}/{i:08d}.jpg",
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
            db.add(fr)
            db.flush()
            seq_to_id[i] = fr.id
        return pid, seq_to_id


def _tile_count(html: str) -> int:
    return html.count('id="frame-tile-')


def test_batch_first_page_has_60_and_sentinel(admin_client: TestClient) -> None:
    pid, _ = _seed(75)
    html = admin_client.get(f"/frames/batch?project_id={pid}").text
    assert _tile_count(html) == 60
    assert "frame-sentinel" in html
    assert "frame-end-cap" not in html
    # Newest-first: the highest sequence (#74) is in this batch, #0 is not.
    assert "#74" in html


def test_batch_before_returns_older_and_endcap(admin_client: TestClient) -> None:
    pid, _ = _seed(75)
    # before=15 -> frames seq 14..0 (15 of them) -> end-cap, no sentinel.
    html = admin_client.get(f"/frames/batch?project_id={pid}&before=15").text
    assert _tile_count(html) == 15
    assert "frame-end-cap" in html
    assert "frame-sentinel" not in html


def test_batch_endcap_for_small_project(admin_client: TestClient) -> None:
    pid, _ = _seed(3)
    html = admin_client.get(f"/frames/batch?project_id={pid}").text
    assert _tile_count(html) == 3
    assert "frame-end-cap" in html
    assert "frame-sentinel" not in html


def test_keyset_stable_under_soft_delete(admin_client: TestClient) -> None:
    """A frame deleted mid-scroll must not skip/duplicate others (keyset)."""
    pid, seq_to_id = _seed(75)
    # Soft-delete a frame that lives in the second batch (seq 5).
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        fr = db.get(Frame, seq_to_id[5])
        assert fr is not None
        fr.lifecycle_state = "soft_deleted"
    # The older batch (before=15) should now have 14 frames, with #5 absent and
    # every other seq 0..14 present exactly once.
    html = admin_client.get(f"/frames/batch?project_id={pid}&before=15").text
    assert _tile_count(html) == 14
    assert f'id="frame-tile-{seq_to_id[5]}"' not in html
    for seq in (0, 4, 6, 14):
        assert f'id="frame-tile-{seq_to_id[seq]}"' in html


def test_frames_page_grid_has_sentinel(admin_client: TestClient) -> None:
    pid, _ = _seed(75)
    html = admin_client.get(f"/frames?project_id={pid}").text
    assert 'id="frame-grid"' in html
    assert "frame-sentinel" in html
    assert _tile_count(html) == 60


def test_batch_nonexistent_project_404(admin_client: TestClient) -> None:
    assert admin_client.get("/frames/batch?project_id=999999").status_code == 404
