"""Web tests for the bulk timestamp-offset endpoint POST /frames/offset.

Covers the ids-only signed-offset contract:
- role-gating (viewer 403) and CSRF (token required).
- the JSON summary shape: shifted / skipped-null / failed counts + ids.
- the inverse-offset Undo (negated seconds over the shifted ids only).
- null-timestamp frames skipped and reported, not failed.
- the over-ceiling rejection and bad/empty input rejection.
- materialize-first: a range descriptor is NOT accepted (ids only).

Seed helpers write directly to the running app's session factory via
``get_context()``, mirroring ``test_frames_bulk_routes.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Frame, Project
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


def _seed_frame(project_id: int, *, seq: int, null_ts: bool = False) -> int:
    ctx = get_context()
    ts = (
        None
        if null_ts
        else datetime(2026, 1, 1, 12, 0, tzinfo=UTC).replace(tzinfo=None)
    )
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


def _capture_ts(frame_id: int) -> object:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        frame = db.get(Frame, frame_id)
        assert frame is not None
        return frame.capture_timestamp


class TestOffsetRoleGatingAndCsrf:
    def test_viewer_forbidden(self, viewer_client: TestClient) -> None:
        pid = _seed_project(name="Offset Viewer")
        fid = _seed_frame(pid, seq=1)
        before = _capture_ts(fid)
        csrf = csrf_of(viewer_client, "/frames")
        resp = viewer_client.post(
            "/frames/offset",
            data={"frame_ids": str(fid), "seconds": "3600", "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 403
        assert _capture_ts(fid) == before

    def test_missing_csrf_rejected(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Offset CSRF")
        fid = _seed_frame(pid, seq=1)
        before = _capture_ts(fid)
        resp = operator_client.post(
            "/frames/offset",
            data={"frame_ids": str(fid), "seconds": "3600"},
            headers=_FORM,
        )
        assert resp.status_code == 403
        assert _capture_ts(fid) == before

    def test_csrf_via_header(self, operator_client: TestClient) -> None:
        """The HTMX path sends the token in the X-CSRF-Token header, not the form."""
        pid = _seed_project(name="Offset Header")
        fid = _seed_frame(pid, seq=1)
        before = _capture_ts(fid)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/offset",
            data={"frame_ids": str(fid), "seconds": "3600"},
            headers={**_FORM, "X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200
        from datetime import timedelta

        assert _capture_ts(fid) == before + timedelta(seconds=3600)


class TestOffsetSummaryShape:
    def test_shift_summary_and_inverse_undo(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Offset Shift")
        ids = [_seed_frame(pid, seq=i) for i in (1, 2, 3)]
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/offset",
            data={
                "frame_ids": ",".join(str(i) for i in ids),
                "seconds": "1800",
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["operation"] == "offset"
        assert body["seconds"] == 1800
        assert body["shifted"] == 3
        assert body["skipped_null"] == 0
        assert body["failed"] == 0
        assert body["shifted_ids"] == ids
        # Undo is the inverse offset (negated seconds) over the shifted ids only.
        assert body["undo"]["operation"] == "offset"
        assert body["undo"]["seconds"] == -1800
        assert body["undo"]["frame_ids"] == ids

    def test_inverse_undo_round_trips_timestamps(
        self, operator_client: TestClient
    ) -> None:
        pid = _seed_project(name="Offset Roundtrip")
        ids = [_seed_frame(pid, seq=i) for i in (1, 2)]
        original = {fid: _capture_ts(fid) for fid in ids}
        csrf = csrf_of(operator_client, "/frames")
        joined = ",".join(str(i) for i in ids)
        # Apply a negative shift, then replay the undo the response prescribes.
        apply_resp = operator_client.post(
            "/frames/offset",
            data={"frame_ids": joined, "seconds": "-3600", "csrf_token": csrf},
            headers=_FORM,
        )
        undo = apply_resp.json()["undo"]
        operator_client.post(
            "/frames/offset",
            data={
                "frame_ids": ",".join(str(i) for i in undo["frame_ids"]),
                "seconds": str(undo["seconds"]),
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        for fid in ids:
            assert _capture_ts(fid) == original[fid]

    def test_null_timestamp_skipped_not_failed(
        self, operator_client: TestClient
    ) -> None:
        pid = _seed_project(name="Offset Null")
        timed = _seed_frame(pid, seq=1)
        untimed = _seed_frame(pid, seq=2, null_ts=True)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/offset",
            data={
                "frame_ids": f"{timed},{untimed}",
                "seconds": "600",
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        body = resp.json()
        assert body["shifted_ids"] == [timed]
        assert body["skipped_null_ids"] == [untimed]
        assert body["failed_ids"] == []
        # The null-timestamp frame stays null (untouched).
        assert _capture_ts(untimed) is None

    def test_missing_id_reported_failed(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Offset Missing")
        fid = _seed_frame(pid, seq=1)
        bad = 9_999_999
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/offset",
            data={
                "frame_ids": f"{fid},{bad}",
                "seconds": "60",
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        body = resp.json()
        assert body["shifted_ids"] == [fid]
        assert body["failed_ids"] == [bad]


class TestOffsetCeilingAndValidation:
    def test_over_ceiling_rejected(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Offset Ceiling")
        fid = _seed_frame(pid, seq=1)
        before = _capture_ts(fid)
        csrf = csrf_of(operator_client, "/frames")
        # 501 ids (one real, padded) exceeds the 500 ceiling.
        ids = ",".join([str(fid)] + [str(1_000_000 + i) for i in range(500)])
        resp = operator_client.post(
            "/frames/offset",
            data={"frame_ids": ids, "seconds": "60", "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 400
        assert "Too many frames" in resp.json()["error"]
        # Nothing was applied.
        assert _capture_ts(fid) == before

    def test_non_integer_seconds_rejected(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Offset BadSeconds")
        fid = _seed_frame(pid, seq=1)
        before = _capture_ts(fid)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/offset",
            data={"frame_ids": str(fid), "seconds": "1h", "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 400
        assert "seconds" in resp.json()["error"]
        assert _capture_ts(fid) == before

    def test_empty_selection_rejected(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/offset",
            data={"frame_ids": "", "seconds": "60", "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 400
        assert "No frames selected" in resp.json()["error"]

    def test_descriptor_is_not_accepted(self, operator_client: TestClient) -> None:
        """Materialize-first: a raw range descriptor is ignored; ids-only.

        Offsetting a selection defined by the timestamps being changed is
        self-referential, so the route reads only ``frame_ids``. A request that
        carries a ``descriptor`` but no ``frame_ids`` resolves to an empty
        selection and is rejected -- the descriptor is never resolved here.
        """
        import json

        pid = _seed_project(name="Offset Descriptor")
        _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        descriptor = json.dumps({"scope": "in_project", "project_id": pid})
        resp = operator_client.post(
            "/frames/offset",
            data={"descriptor": descriptor, "seconds": "60", "csrf_token": csrf},
            headers=_FORM,
        )
        # No frame_ids field => empty selection => rejected; descriptor untouched.
        assert resp.status_code == 400
        assert "No frames selected" in resp.json()["error"]
