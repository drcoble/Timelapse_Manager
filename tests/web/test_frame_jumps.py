"""Web/integration tests for the frames-browser jump controls.

Exercises ``GET /frames?jump=...`` end to end: the Start/Newest series-end
jumps, the Next/Prev-gap jumps relative to ``?at=``, the nearest-frame note when
``?at=`` lands between frames, and the single-project-only contract. The gap is
seeded deliberately and the campaign span pinned so detection is deterministic
and lands on the same lapse the ribbon would draw.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.routers.frames import _resolve_jump

# A contiguous early run (seq 0..9 at 5-min steps), a long lapse, then a later
# run (seq 10..19). The lapse is ~70% of the campaign span -> well over the 4%
# ribbon threshold, so detection is unambiguous.
_T0 = datetime(2026, 1, 1, tzinfo=UTC)
_STEP = timedelta(minutes=5)
_GAP = timedelta(hours=2)
# The single gap is bounded by frame seq 9 (before) and seq 10 (after).
_BEFORE_GAP = _T0 + _STEP * 9
_AFTER_GAP = _BEFORE_GAP + _GAP


def _naive(t: datetime) -> datetime:
    return t.replace(tzinfo=None)


def _seed_gapped() -> tuple[int, dict[int, int]]:
    """Seed a project with a deliberate capture gap; pin start/end to the run.

    Returns ``(project_id, {seq: frame_id})``.
    """
    ctx = get_context()
    seq_to_id: dict[int, int] = {}
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="gap-cam", address="10.0.0.7", protocol="vapix")
        db.add(cam)
        db.flush()
        last_time = _AFTER_GAP + _STEP * 9
        proj = Project(
            camera_id=cam.id,
            name="Gap Project",
            lifecycle_state="active",
            start_date=_naive(_T0),
            end_date=_naive(last_time),
        )
        db.add(proj)
        db.flush()
        pid = proj.id
        times: list[datetime] = [_T0 + _STEP * i for i in range(10)]
        times += [_AFTER_GAP + _STEP * i for i in range(10)]
        for i, t in enumerate(times):
            fr = Frame(
                project_id=pid,
                sequence_index=i,
                capture_timestamp=_naive(t),
                file_path=f"/frames/{pid}/{i:08d}.jpg",
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
            db.add(fr)
            db.flush()
            seq_to_id[i] = fr.id
        return pid, seq_to_id


def _seed_n(n: int) -> tuple[int, dict[int, int]]:
    """Seed a project with ``n`` contiguous frames; return (pid, {seq: id})."""
    ctx = get_context()
    seq_to_id: dict[int, int] = {}
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="n-cam", address="10.0.0.10", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(camera_id=cam.id, name="N Project", lifecycle_state="active")
        db.add(proj)
        db.flush()
        pid = proj.id
        for i in range(n):
            fr = Frame(
                project_id=pid,
                sequence_index=i,
                capture_timestamp=_naive(_T0 + _STEP * i),
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


def _tile_ids(html: str, seq_to_id: dict[int, int]) -> set[int]:
    """Return the set of seeded sequence indices whose tile is in the HTML."""
    return {seq for seq, fid in seq_to_id.items() if f'id="frame-tile-{fid}"' in html}


# --- jump=start / jump=newest ----------------------------------------------


def test_jump_start_lands_on_oldest_with_endcap(admin_client: TestClient) -> None:
    pid, seq_to_id = _seed_gapped()
    html = admin_client.get(f"/frames?project_id={pid}&jump=start").text
    present = _tile_ids(html, seq_to_id)
    # The oldest frame (seq 0) is in the batch and the series-start end-cap shows.
    assert 0 in present
    assert "frame-end-cap" in html
    assert "frame-sentinel" not in html


def test_jump_start_returns_oldest_page_not_single_frame(
    admin_client: TestClient,
) -> None:
    """75 frames: Start returns the oldest *page* (60), not just the oldest tile."""
    pid, seq_to_id = _seed_n(75)
    html = admin_client.get(f"/frames?project_id={pid}&jump=start").text
    # A full page of the oldest frames, newest-first within the page: seq 0..59
    # present, the newest (seq 74) absent, end-cap (series start) shown.
    assert _tile_count(html) == 60
    present = _tile_ids(html, seq_to_id)
    assert 0 in present
    assert 59 in present
    assert 74 not in present
    assert "frame-end-cap" in html
    assert "frame-sentinel" not in html


def test_jump_newest_is_newest_first(admin_client: TestClient) -> None:
    pid, seq_to_id = _seed_gapped()
    html = admin_client.get(f"/frames?project_id={pid}&jump=newest").text
    present = _tile_ids(html, seq_to_id)
    # The newest frame (seq 19) is present; the bare grid matches no-jump.
    assert 19 in present
    plain = admin_client.get(f"/frames?project_id={pid}").text
    assert _tile_ids(plain, seq_to_id) == present


# --- jump=next_gap / jump=prev_gap -----------------------------------------


def test_next_gap_lands_on_last_frame_before_gap(admin_client: TestClient) -> None:
    pid, seq_to_id = _seed_gapped()
    # Anchor in the early run (before the gap); next gap is the only one.
    at = _naive(_T0 + _STEP * 2).isoformat()
    html = admin_client.get(f"/frames?project_id={pid}&at={at}&jump=next_gap").text
    present = _tile_ids(html, seq_to_id)
    # The window centres on the last frame before the gap (seq 9).
    assert 9 in present


def test_prev_gap_lands_on_last_frame_before_gap(admin_client: TestClient) -> None:
    pid, seq_to_id = _seed_gapped()
    # Anchor in the later run (after the gap); the prev gap is the only one.
    at = _naive(_AFTER_GAP + _STEP * 5).isoformat()
    html = admin_client.get(f"/frames?project_id={pid}&at={at}&jump=prev_gap").text
    present = _tile_ids(html, seq_to_id)
    assert 9 in present


def test_gap_jump_threads_gap_context(migrated_factory) -> None:
    """The next/prev-gap path returns the gap band context (raw datetimes)."""
    with session_scope(migrated_factory) as db:
        cam = Camera(name="gap-ctx-cam", address="10.0.0.8", protocol="vapix")
        db.add(cam)
        db.flush()
        last_time = _AFTER_GAP + _STEP * 9
        proj = Project(
            camera_id=cam.id,
            name="Gap Ctx",
            lifecycle_state="active",
            start_date=_naive(_T0),
            end_date=_naive(last_time),
        )
        db.add(proj)
        db.flush()
        times = [_T0 + _STEP * i for i in range(10)]
        times += [_AFTER_GAP + _STEP * i for i in range(10)]
        for i, t in enumerate(times):
            db.add(
                Frame(
                    project_id=proj.id,
                    sequence_index=i,
                    capture_timestamp=_naive(t),
                    file_path=f"/frames/{i:08d}.jpg",
                    capture_status="captured",
                    origin="captured",
                    lifecycle_state="active",
                )
            )
        db.flush()

        _frames, _next, extra = _resolve_jump(
            db,
            proj.id,
            project=proj,
            jump="next_gap",
            anchor=_T0 + _STEP * 2,
            include_deleted=False,
        )
        gap = extra["preceding_gap"]
        assert gap is not None
        # Raw aware datetimes for tz-aware template rendering -- not strings.
        assert isinstance(gap["start"], datetime)
        assert gap["start"].tzinfo is not None
        assert gap["start"] == _BEFORE_GAP
        assert gap["end"] == _AFTER_GAP
        assert gap["duration_seconds"] == int(_GAP.total_seconds())
        assert gap["frame_count"] == 0


def test_gap_jump_no_gap_in_direction_falls_back(admin_client: TestClient) -> None:
    pid, seq_to_id = _seed_gapped()
    # Anchor in the later run; there is no gap *after* it -> newest batch, no 500.
    at = _naive(_AFTER_GAP + _STEP * 5).isoformat()
    resp = admin_client.get(f"/frames?project_id={pid}&at={at}&jump=next_gap")
    assert resp.status_code == 200
    assert 19 in _tile_ids(resp.text, seq_to_id)


# --- nearest-frame note -----------------------------------------------------


def test_nearest_frame_note_when_at_between_frames(migrated_factory) -> None:
    """An off-frame ?at= surfaces the nearest captured frame's timestamp."""
    with session_scope(migrated_factory) as db:
        cam = Camera(name="near-cam", address="10.0.0.9", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(camera_id=cam.id, name="Near", lifecycle_state="active")
        db.add(proj)
        db.flush()
        for i in range(10):
            db.add(
                Frame(
                    project_id=proj.id,
                    sequence_index=i,
                    capture_timestamp=_naive(_T0 + _STEP * i),
                    file_path=f"/frames/{i:08d}.jpg",
                    capture_status="captured",
                    origin="captured",
                    lifecycle_state="active",
                )
            )
        db.flush()

        # 30s past frame 3 -> grid centres on frame 4; note shows frame 4's time.
        off = _T0 + _STEP * 3 + timedelta(seconds=30)
        _f, _n, extra = _resolve_jump(
            db, proj.id, project=proj, jump=None, anchor=off, include_deleted=False
        )
        note = extra["nearest_frame_note"]
        assert note is not None
        assert note == _T0 + _STEP * 4

        # An exact anchor surfaces no note.
        _f2, _n2, extra2 = _resolve_jump(
            db,
            proj.id,
            project=proj,
            jump=None,
            anchor=_T0 + _STEP * 3,
            include_deleted=False,
        )
        assert extra2["nearest_frame_note"] is None


# --- single-project only ----------------------------------------------------


def test_jump_ignored_on_all_projects_grid(admin_client: TestClient) -> None:
    """Without project_id the jump is ignored: the All-Projects grid renders."""
    _seed_gapped()
    resp = admin_client.get("/frames?jump=start")
    assert resp.status_code == 200
    # The global grid is keyset on frame id; no jump resolution applies. The
    # project picker (an All-Projects-only affordance) confirms the global path.
    assert 'id="frame-grid"' in resp.text
