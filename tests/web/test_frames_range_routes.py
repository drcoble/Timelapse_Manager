"""Web tests for the range-descriptor routes and the descriptor bulk path.

Covers:
- POST /frames/range/count returns an estimate equal to the size a bulk over the
  same descriptor would act on.
- POST /frames/range/materialize returns the right concrete id list.
- POST /frames/bulk with a descriptor applies the op to the whole resolved set,
  minus deselected ids.
- The over-ceiling descriptor bulk -> documented error-shaped summary (status 200).
- Role-gating (viewer 403) and CSRF on the range routes.

Seeding mirrors test_frames_bulk_routes.py (direct writes via get_context()).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_T0 = datetime(2026, 3, 1, tzinfo=UTC)
_STEP = timedelta(hours=1)


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


def _seed_frame(project_id: int, *, seq: int, timed: bool = True) -> int:
    ctx = get_context()
    ts = (_T0 + _STEP * seq).replace(tzinfo=None) if timed else None
    with session_scope(ctx.session_factory) as db:
        frame = Frame(
            project_id=project_id,
            sequence_index=seq,
            capture_timestamp=ts,
            file_path=f"/frames/{project_id}/{seq:08d}.jpg",
            capture_status="captured",
            origin="captured",
            lifecycle_state="active",
        )
        db.add(frame)
        db.flush()
        return frame.id


def _frame_excluded(frame_id: int) -> object:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        frame = db.get(Frame, frame_id)
        assert frame is not None
        return frame.excluded_at


def _descriptor(pid: int, **overrides) -> dict:
    desc = {
        "scope": "in_range",
        "project_id": pid,
        "time_range": {"from": None, "to": None},
        "filters": {"include_deleted": False},
        "deselected_ids": [],
    }
    desc.update(overrides)
    return desc


class TestRangeCount:
    def test_count_equals_resolved_bulk_size(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Range Count")
        timed = [_seed_frame(pid, seq=i) for i in range(5)]
        _seed_frame(pid, seq=99, timed=False)  # null-ts: off the in_range axis
        csrf = csrf_of(operator_client, "/frames")
        desc = _descriptor(pid)
        resp = operator_client.post(
            "/frames/range/count",
            data={"descriptor": json.dumps(desc), "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        # The estimate counts only the 5 timed frames, not the null-ts one.
        assert resp.json() == {"count": 5}

        # A bulk over the same descriptor acts on exactly that many frames.
        operator_client.post(
            "/frames/bulk",
            data={
                "operation": "exclude",
                "descriptor": json.dumps(desc),
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        excluded = [t for t in timed if _frame_excluded(t) is not None]
        assert len(excluded) == 5

    def test_count_subtracts_deselected(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Range Deselect")
        timed = [_seed_frame(pid, seq=i) for i in range(4)]
        csrf = csrf_of(operator_client, "/frames")
        desc = _descriptor(pid, deselected_ids=[timed[0], timed[1]])
        resp = operator_client.post(
            "/frames/range/count",
            data={"descriptor": json.dumps(desc), "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.json() == {"count": 2}

    def test_bad_descriptor_is_400(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/range/count",
            data={"descriptor": "{not json", "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 400

    def test_missing_project_is_404(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/frames")
        desc = _descriptor(9_999_999)
        resp = operator_client.post(
            "/frames/range/count",
            data={"descriptor": json.dumps(desc), "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 404


class TestRangeMaterialize:
    def test_materialize_returns_sorted_ids(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Range Mat")
        timed = [_seed_frame(pid, seq=i) for i in range(3)]
        _seed_frame(pid, seq=50, timed=False)  # null-ts excluded by in_range
        csrf = csrf_of(operator_client, "/frames")
        desc = _descriptor(pid)
        resp = operator_client.post(
            "/frames/range/materialize",
            data={"descriptor": json.dumps(desc), "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 200
        assert resp.json() == {"frame_ids": sorted(timed)}


class TestDescriptorBulk:
    def test_descriptor_bulk_applies_to_all_in_range(
        self, operator_client: TestClient
    ) -> None:
        pid = _seed_project(name="Desc Bulk")
        timed = [_seed_frame(pid, seq=i) for i in range(4)]
        null_ts = _seed_frame(pid, seq=80, timed=False)
        csrf = csrf_of(operator_client, "/frames")
        desc = _descriptor(pid, deselected_ids=[timed[0]])
        resp = operator_client.post(
            "/frames/bulk",
            data={
                "operation": "exclude",
                "descriptor": json.dumps(desc),
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        assert resp.status_code == 200
        # Deselected frame untouched; the null-ts frame is off the in_range axis.
        assert _frame_excluded(timed[0]) is None
        assert _frame_excluded(null_ts) is None
        # The remaining three in-range frames are excluded.
        for fid in timed[1:]:
            assert _frame_excluded(fid) is not None
        assert "3 excluded" in resp.text

    def test_both_ids_and_descriptor_rejected(
        self, operator_client: TestClient
    ) -> None:
        pid = _seed_project(name="Desc Both")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/bulk",
            data={
                "operation": "exclude",
                "frame_ids": str(fid),
                "descriptor": json.dumps(_descriptor(pid)),
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        assert resp.status_code == 200
        assert "not both" in resp.text
        assert _frame_excluded(fid) is None

    def test_over_ceiling_descriptor_rejected(
        self, operator_client: TestClient
    ) -> None:
        pid = _seed_project(name="Desc Ceiling")
        # 501 timed frames exceeds the 500 sync ceiling.
        for i in range(501):
            _seed_frame(pid, seq=i)
        csrf = csrf_of(operator_client, "/frames")
        desc = _descriptor(pid)
        resp = operator_client.post(
            "/frames/bulk",
            data={
                "operation": "exclude",
                "descriptor": json.dumps(desc),
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        assert resp.status_code == 200
        assert "Too many frames" in resp.text
        # Nothing applied.
        with session_scope(get_context().session_factory) as db:
            any_excluded = (
                db.query(Frame)
                .filter(Frame.project_id == pid)
                .filter(Frame.excluded_at.is_not(None))
                .count()
            )
        assert any_excluded == 0


class TestRangeRoleGatingAndCsrf:
    def test_count_viewer_forbidden(self, viewer_client: TestClient) -> None:
        pid = _seed_project(name="Range Viewer")
        _seed_frame(pid, seq=1)
        csrf = csrf_of(viewer_client, "/frames")
        resp = viewer_client.post(
            "/frames/range/count",
            data={"descriptor": json.dumps(_descriptor(pid)), "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 403

    def test_materialize_viewer_forbidden(self, viewer_client: TestClient) -> None:
        pid = _seed_project(name="Mat Viewer")
        _seed_frame(pid, seq=1)
        csrf = csrf_of(viewer_client, "/frames")
        resp = viewer_client.post(
            "/frames/range/materialize",
            data={"descriptor": json.dumps(_descriptor(pid)), "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 403

    def test_count_missing_csrf_rejected(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Range NoCsrf")
        _seed_frame(pid, seq=1)
        resp = operator_client.post(
            "/frames/range/count",
            data={"descriptor": json.dumps(_descriptor(pid))},
            headers=_FORM,
        )
        assert resp.status_code == 403

    def test_count_csrf_via_header(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Range Header")
        _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/range/count",
            data={"descriptor": json.dumps(_descriptor(pid))},
            headers={**_FORM, "X-CSRF-Token": csrf},
        )
        assert resp.status_code == 200
        assert resp.json() == {"count": 1}
