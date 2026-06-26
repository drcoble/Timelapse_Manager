"""Web tests for the bulk lifecycle endpoint POST /frames/bulk.

Covers the four uniform operations (delete/restore/exclude/include) over an
explicit id-set:
- skip-not-raise: a non-existent id is reported in the result, the rest succeed.
- role-gating (viewer 403) and CSRF (token required).
- the over-ceiling rejection (an error-shaped summary, status 200).
- undo: the inverse operation round-trips state.
- one audit Event per affected frame (failed ids get none).

Seed helpers write directly to the running app's session factory via
``get_context()``, mirroring ``test_frame_exclude_routes.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

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
    ts = datetime(2026, 1, 1, 0, seq % 60, tzinfo=UTC).replace(tzinfo=None)
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


def _frame_state(frame_id: int) -> tuple[str, object]:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        frame = db.get(Frame, frame_id)
        assert frame is not None
        return frame.lifecycle_state, frame.excluded_at


def _events_for(project_id: int) -> list[Event]:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return (
            db.query(Event)
            .filter(Event.scope == "project")
            .filter(Event.scope_id == project_id)
            .all()
        )


class TestBulkRoleGatingAndCsrf:
    def test_viewer_forbidden(self, viewer_client: TestClient) -> None:
        pid = _seed_project(name="Bulk Viewer")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(viewer_client, "/frames")
        resp = viewer_client.post(
            "/frames/bulk",
            data={"operation": "exclude", "frame_ids": str(fid), "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 403
        _state, excluded_at = _frame_state(fid)
        assert excluded_at is None

    def test_missing_csrf_rejected(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Bulk CSRF")
        fid = _seed_frame(pid, seq=1)
        resp = operator_client.post(
            "/frames/bulk",
            data={"operation": "exclude", "frame_ids": str(fid)},
            headers=_FORM,
        )
        assert resp.status_code == 403
        _state, excluded_at = _frame_state(fid)
        assert excluded_at is None

    def test_csrf_via_header(self, operator_client: TestClient) -> None:
        """The HTMX path sends the token in the X-CSRF-Token header, not the form."""
        pid = _seed_project(name="Bulk Header")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/bulk",
            data={"operation": "exclude", "frame_ids": str(fid)},
            headers={**_FORM, "X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200
        _state, excluded_at = _frame_state(fid)
        assert excluded_at is not None


class TestBulkOperationsSkipNotRaise:
    def test_exclude_mixed_with_bad_id(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Bulk Exc")
        good = [_seed_frame(pid, seq=i) for i in (1, 2, 3)]
        bad = 9_999_999
        ids = ",".join(str(i) for i in [good[0], bad, good[1], good[2]])
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/bulk",
            data={"operation": "exclude", "frame_ids": ids, "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        html = resp.text
        # All three good frames are excluded.
        for fid in good:
            _state, excluded_at = _frame_state(fid)
            assert excluded_at is not None
        # The bad id is surfaced in the result (failed), and "3 excluded" shows.
        assert "3 excluded" in html
        assert str(bad) in html
        # Undo carries the inverse op over only the succeeded ids.
        assert 'data-bulk-operation="include"' in html

    def test_delete_then_restore_roundtrips(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Bulk Del")
        ids = [_seed_frame(pid, seq=i) for i in (10, 11)]
        csrf = csrf_of(operator_client, "/frames")
        joined = ",".join(str(i) for i in ids)
        operator_client.post(
            "/frames/bulk",
            data={"operation": "delete", "frame_ids": joined, "csrf_token": csrf},
            headers=_FORM,
        )
        for fid in ids:
            state, _exc = _frame_state(fid)
            assert state == "soft_deleted"
        # Undo = restore over the same ids round-trips the state.
        resp = operator_client.post(
            "/frames/bulk",
            data={"operation": "restore", "frame_ids": joined, "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        for fid in ids:
            state, _exc = _frame_state(fid)
            assert state == "active"

    def test_include_clears_exclusion(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Bulk Inc")
        ids = [_seed_frame(pid, seq=i) for i in (20, 21)]
        csrf = csrf_of(operator_client, "/frames")
        joined = ",".join(str(i) for i in ids)
        operator_client.post(
            "/frames/bulk",
            data={"operation": "exclude", "frame_ids": joined, "csrf_token": csrf},
            headers=_FORM,
        )
        operator_client.post(
            "/frames/bulk",
            data={"operation": "include", "frame_ids": joined, "csrf_token": csrf},
            headers=_FORM,
        )
        for fid in ids:
            _state, excluded_at = _frame_state(fid)
            assert excluded_at is None


class TestBulkAuditEvents:
    def test_one_event_per_succeeded_frame(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Bulk Audit")
        good = [_seed_frame(pid, seq=i) for i in (1, 2)]
        bad = 9_999_998
        csrf = csrf_of(operator_client, "/frames")
        ids = ",".join(str(i) for i in [good[0], bad, good[1]])
        operator_client.post(
            "/frames/bulk",
            data={"operation": "exclude", "frame_ids": ids, "csrf_token": csrf},
            headers=_FORM,
        )
        events = _events_for(pid)
        exclude_events = [
            e for e in events if e.event_metadata.get("action") == "exclude"
        ]
        # One event per succeeded frame; the missing id produces no event.
        assert len(exclude_events) == 2
        affected = {e.event_metadata.get("frame_id") for e in exclude_events}
        assert affected == set(good)


class TestBulkCeilingAndValidation:
    def test_over_ceiling_rejected(self, operator_client: TestClient) -> None:
        """A selection past the sync ceiling is rejected with an error summary."""
        pid = _seed_project(name="Bulk Ceiling")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        # 501 ids (one real, padded) exceeds the 500 ceiling.
        ids = ",".join([str(fid)] + [str(1_000_000 + i) for i in range(500)])
        resp = operator_client.post(
            "/frames/bulk",
            data={"operation": "exclude", "frame_ids": ids, "csrf_token": csrf},
            headers=_FORM,
        )
        # Documented behaviour: status 200 with an error-shaped summary so HTMX
        # swaps the message into the bar.
        assert resp.status_code == 200
        assert "Too many frames" in resp.text
        # Nothing was applied.
        _state, excluded_at = _frame_state(fid)
        assert excluded_at is None

    def test_unknown_operation_rejected(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Bulk BadOp")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/bulk",
            data={"operation": "nuke", "frame_ids": str(fid), "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        assert "Unknown bulk operation" in resp.text
        _state, excluded_at = _frame_state(fid)
        assert excluded_at is None

    def test_empty_selection_rejected(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/bulk",
            data={"operation": "exclude", "frame_ids": "", "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        assert "No frames selected" in resp.text


class TestBulkResponseShape:
    def test_small_set_emits_oob_tiles(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Bulk OOB")
        ids = [_seed_frame(pid, seq=i) for i in (1, 2)]
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/bulk",
            data={
                "operation": "exclude",
                "frame_ids": ",".join(str(i) for i in ids),
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        html = resp.text
        # Affected tiles re-render out-of-band so the grid updates in place.
        assert "hx-swap-oob" in html
        for fid in ids:
            assert f'id="frame-tile-{fid}"' in html
        # A small set does not flag a window reload.
        assert 'data-bulk-reload-window="0"' in html
