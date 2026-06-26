"""Web tests for frames time-navigation: the ``?at=`` centered window, the
date-jump form, and the new-frames count endpoint.

These exercise the single-project time axis (sequence_index space anchored at a
timestamp-resolved boundary) and the global-vs-single scope of ``/frames/since``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

# A fixed origin so anchors are computable from a sequence offset.
_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_STEP = timedelta(minutes=5)


def _seed_timed(
    n_frames: int, *, name: str = "TN Project"
) -> tuple[int, dict[int, int]]:
    """Seed a project with ``n`` frames, seq ``i`` captured at ``_T0 + i*_STEP``.

    Returns ``(project_id, {seq: frame_id})``. Distinct, monotonically-spaced
    timestamps make a timestamp anchor resolve to an unambiguous sequence index.
    """
    ctx = get_context()
    seq_to_id: dict[int, int] = {}
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="10.0.0.7", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(camera_id=cam.id, name=name, lifecycle_state="active")
        db.add(proj)
        db.flush()
        pid = proj.id
        for i in range(n_frames):
            ts = (_T0 + _STEP * i).replace(tzinfo=None)
            fr = Frame(
                project_id=pid,
                sequence_index=i,
                capture_timestamp=ts,
                file_path=f"/frames/{pid}/{i:08d}.jpg",
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
            db.add(fr)
            db.flush()
            seq_to_id[i] = fr.id
        return pid, seq_to_id


def _anchor_iso(seq: int) -> str:
    """ISO-8601 (no offset) capture time of frame ``seq`` — the datetime-local form."""
    return (_T0 + _STEP * seq).replace(tzinfo=None).isoformat()


def _tile_count(html: str) -> int:
    return html.count('id="frame-tile-')


def _present_seqs(html: str, seq_to_id: dict[int, int]) -> set[int]:
    return {s for s, fid in seq_to_id.items() if f'id="frame-tile-{fid}"' in html}


def test_frames_at_window_centers_around_anchor(admin_client: TestClient) -> None:
    """?at= returns frames both NEWER and OLDER than the anchor (centered)."""
    pid, seq_to_id = _seed_timed(100)
    # Anchor at seq 50: window is seq 20..49 (before) + seq 50..79 (at-or-after).
    html = admin_client.get(f"/frames?project_id={pid}&at={_anchor_iso(50)}").text
    present = _present_seqs(html, seq_to_id)
    assert 50 in present  # the boundary itself (at-or-after)
    assert max(present) > 50  # newer than the anchor present
    assert min(present) < 50  # older than the anchor present
    # 30 per side, so 60 tiles around a mid-series anchor.
    assert _tile_count(html) == 60


def test_frames_at_window_has_usable_before_cursor(admin_client: TestClient) -> None:
    """The window yields a before= sentinel that continues the scroll seamlessly."""
    pid, seq_to_id = _seed_timed(100)
    html = admin_client.get(f"/frames?project_id={pid}&at={_anchor_iso(50)}").text
    # Oldest in the window is seq 20; the sentinel's before cursor must be 20 so
    # the next batch is seq 19..0 with no skip/overlap.
    assert "frame-sentinel" in html
    assert "before=20" in html
    older = admin_client.get(f"/frames/batch?project_id={pid}&before=20").text
    older_seqs = _present_seqs(older, seq_to_id)
    assert older_seqs == set(range(0, 20))  # exactly the older remainder
    assert 20 not in older_seqs  # no overlap with the window's oldest


def test_frames_at_window_near_first_frame_truncates_before(
    admin_client: TestClient,
) -> None:
    """Near the series start the before-half is short; no error, reaches the cap."""
    pid, seq_to_id = _seed_timed(100)
    # Anchor at seq 5: only seq 0..4 exist before it (5 < the 30 cap).
    html = admin_client.get(f"/frames?project_id={pid}&at={_anchor_iso(5)}").text
    present = _present_seqs(html, seq_to_id)
    assert min(present) == 0  # series start reached
    assert 5 in present
    # 5 before + 30 at-or-after (seq 5..34) = 35 tiles; before-half not padded.
    assert _tile_count(html) == 35
    assert "frame-end-cap" in html  # start of series reached on the before side
    assert "frame-sentinel" not in html


def test_frames_at_window_near_last_frame_truncates_after(
    admin_client: TestClient,
) -> None:
    """Near the series end the at-or-after half is short; no error."""
    pid, seq_to_id = _seed_timed(100)
    # Anchor at seq 95: only seq 95..99 (5) are at-or-after; 30 before (65..94).
    html = admin_client.get(f"/frames?project_id={pid}&at={_anchor_iso(95)}").text
    present = _present_seqs(html, seq_to_id)
    assert max(present) == 99  # newest frame included
    assert 95 in present
    assert _tile_count(html) == 35  # 5 at-or-after + 30 before


def test_frames_at_past_last_frame_clamps_to_newest(admin_client: TestClient) -> None:
    """An anchor past the last capture clamps to the newest page, never blank."""
    pid, seq_to_id = _seed_timed(50)
    future = (_T0 + _STEP * 1000).replace(tzinfo=None).isoformat()
    html = admin_client.get(f"/frames?project_id={pid}&at={future}").text
    present = _present_seqs(html, seq_to_id)
    assert present  # not empty
    assert 49 in present  # newest frame shown
    # Clamped to the newest batch (30 on this side of the tail).
    assert _tile_count(html) == 30


def test_frames_at_htmx_returns_fragment_not_full_page(
    admin_client: TestClient,
) -> None:
    """The date-jump form (HTMX) gets just the batch fragment, not a nested page."""
    pid, _ = _seed_timed(100)
    resp = admin_client.get(
        f"/frames?project_id={pid}&at={_anchor_iso(50)}",
        headers={"HX-Request": "true"},
    )
    html = resp.text
    assert "frame-tile-" in html
    # A fragment, so no base-layout chrome (no <html>, no full page <head>).
    assert "<html" not in html.lower()
    assert 'id="frame-grid"' not in html  # the grid container belongs to the page


def test_frames_at_ignored_for_all_projects(admin_client: TestClient) -> None:
    """?at= has no time axis under All-Projects; it falls back to newest-first."""
    _seed_timed(100)
    # No project_id -> global grid. An at= must not error and must not window.
    resp = admin_client.get(f"/frames?at={_anchor_iso(50)}")
    assert resp.status_code == 200
    assert "frame-ribbon" not in resp.text  # ribbon hidden under All-Projects


def test_frames_since_counts_newer_by_id(admin_client: TestClient) -> None:
    """/frames/since returns frames with id greater than the cursor, project-scoped."""
    pid, seq_to_id = _seed_timed(10)
    cursor = seq_to_id[6]  # the 7th frame's id
    data = admin_client.get(f"/frames/since?after={cursor}&project_id={pid}").json()
    # Frames seq 7,8,9 have ids greater than seq 6's id -> 3 newer.
    assert data == {"count": 3}


def test_frames_since_zero_at_newest(admin_client: TestClient) -> None:
    pid, seq_to_id = _seed_timed(10)
    newest = seq_to_id[9]
    data = admin_client.get(f"/frames/since?after={newest}&project_id={pid}").json()
    assert data == {"count": 0}


def test_frames_since_respects_project_scope(admin_client: TestClient) -> None:
    """A per-project count excludes another project's newer frames; global includes."""
    pid_a, ids_a = _seed_timed(5, name="A")
    pid_b, ids_b = _seed_timed(5, name="B")  # inserted later -> higher ids
    cursor = ids_a[4]  # A's newest id; all of B is newer than it
    # Scoped to A: nothing in A is newer than A's own newest.
    assert admin_client.get(
        f"/frames/since?after={cursor}&project_id={pid_a}"
    ).json() == {"count": 0}
    # Scoped to B: all 5 of B's frames are newer than A's newest id.
    assert admin_client.get(
        f"/frames/since?after={cursor}&project_id={pid_b}"
    ).json() == {"count": 5}
    # Global (no project_id): all 5 of B count.
    assert admin_client.get(f"/frames/since?after={cursor}").json() == {"count": 5}


def test_frames_since_excludes_soft_deleted(admin_client: TestClient) -> None:
    pid, seq_to_id = _seed_timed(10)
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        fr = db.get(Frame, seq_to_id[8])
        assert fr is not None
        fr.lifecycle_state = "soft_deleted"
    cursor = seq_to_id[6]
    # seq 7,9 active and newer (8 is soft-deleted) -> 2.
    data = admin_client.get(f"/frames/since?after={cursor}&project_id={pid}").json()
    assert data == {"count": 2}
