"""Web tests for the async export endpoints.

* ``POST /frames/export`` -- operator-gated, CSRF-protected; enqueues a
  ``kind="export"`` RenderJob over an explicit id-set or a descriptor and returns
  a job handle.
* ``GET /frames/export/{job_id}`` -- the poll status JSON.
* ``GET /frames/export/{job_id}/download`` -- serves the produced zip, 404 until
  it is ready.

Seed helpers write directly to the running app's session factory via
``get_context()``, mirroring ``test_frames_bulk_routes.py``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Frame, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.render.spec import project_render_root
from timelapse_manager.runtime import get_context

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}


def _seed_project(*, name: str) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
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
            file_path=f"{seq:08d}.jpg",
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


def _latest_export_job(project_id: int) -> RenderJob:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        job = (
            db.query(RenderJob)
            .filter(RenderJob.project_id == project_id, RenderJob.kind == "export")
            .order_by(RenderJob.id.desc())
            .first()
        )
        assert job is not None
        db.expunge(job)
        return job


def _mark_export_done_with_zip(job_id: int, project_id: int) -> Path:
    """Mark an export job done and place a real zip in the render root."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = db.get(Project, project_id)
        assert proj is not None
        root = project_render_root(ctx.settings, proj)
        root.mkdir(parents=True, exist_ok=True)
        zip_path = root / f"export-{job_id}.zip"
        zip_path.write_bytes(b"PK\x03\x04zip-bytes")
        job = db.get(RenderJob, job_id)
        assert job is not None
        job.status = "done"
        job.output_file_path = str(zip_path)
    return zip_path


class TestExportRoleGatingAndCsrf:
    def test_viewer_forbidden(self, viewer_client: TestClient) -> None:
        pid = _seed_project(name="Exp Viewer")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(viewer_client, "/frames")
        resp = viewer_client.post(
            "/frames/export",
            data={"frame_ids": str(fid), "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 403

    def test_missing_csrf_rejected(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exp CSRF")
        fid = _seed_frame(pid, seq=1)
        resp = operator_client.post(
            "/frames/export",
            data={"frame_ids": str(fid)},
            headers=_FORM,
        )
        assert resp.status_code == 403


class TestExportEnqueue:
    def test_explicit_ids_enqueue_job_handle(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exp Ids")
        ids = [_seed_frame(pid, seq=i) for i in (1, 2, 3)]
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/export",
            data={
                "frame_ids": ",".join(str(i) for i in ids),
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending"
        job = _latest_export_job(pid)
        assert body["job_id"] == job.id
        # The selection is pinned onto the job for the builder to read.
        assert job.output_settings == {"frame_ids": ids}

    def test_descriptor_enqueue(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exp Desc")
        ids = [_seed_frame(pid, seq=i) for i in (4, 5)]
        descriptor = json.dumps({"scope": "in_project", "project_id": pid})
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/export",
            data={"descriptor": descriptor, "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 202
        job = _latest_export_job(pid)
        assert sorted(job.output_settings["frame_ids"]) == sorted(ids)

    def test_empty_selection_rejected(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/export",
            data={"frame_ids": "", "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 400
        assert "No frames selected" in resp.json()["error"]

    def test_both_inputs_rejected(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exp Both")
        fid = _seed_frame(pid, seq=1)
        descriptor = json.dumps({"scope": "in_project", "project_id": pid})
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/export",
            data={
                "frame_ids": str(fid),
                "descriptor": descriptor,
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        assert resp.status_code == 400

    def test_cross_project_selection_rejected(
        self, operator_client: TestClient
    ) -> None:
        p1 = _seed_project(name="Exp P1")
        p2 = _seed_project(name="Exp P2")
        f1 = _seed_frame(p1, seq=1)
        f2 = _seed_frame(p2, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        resp = operator_client.post(
            "/frames/export",
            data={"frame_ids": f"{f1},{f2}", "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 400
        assert "single project" in resp.json()["error"]


class TestExportStatus:
    def test_status_shape_for_pending_job(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exp Status")
        ids = [_seed_frame(pid, seq=i) for i in (1, 2)]
        csrf = csrf_of(operator_client, "/frames")
        operator_client.post(
            "/frames/export",
            data={"frame_ids": ",".join(str(i) for i in ids), "csrf_token": csrf},
            headers=_FORM,
        )
        job = _latest_export_job(pid)
        resp = operator_client.get(f"/frames/export/{job.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body == {
            "job_id": job.id,
            "status": "pending",
            "progress": 0,
            "frame_count": 2,
            "ready": False,
        }

    def test_status_unknown_job_404(self, operator_client: TestClient) -> None:
        resp = operator_client.get("/frames/export/999999")
        assert resp.status_code == 404

    def test_status_of_render_job_404(self, operator_client: TestClient) -> None:
        """A non-export render job is invisible through the export surface."""
        pid = _seed_project(name="Exp NotExport")
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            job = RenderJob(project_id=pid, kind="manual", status="pending")
            db.add(job)
            db.flush()
            job_id = job.id
        resp = operator_client.get(f"/frames/export/{job_id}")
        assert resp.status_code == 404


class TestExportDownload:
    def test_download_404_until_done(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exp DlPending")
        fid = _seed_frame(pid, seq=1)
        csrf = csrf_of(operator_client, "/frames")
        operator_client.post(
            "/frames/export",
            data={"frame_ids": str(fid), "csrf_token": csrf},
            headers=_FORM,
        )
        job = _latest_export_job(pid)
        resp = operator_client.get(f"/frames/export/{job.id}/download")
        assert resp.status_code == 404

    def test_download_serves_zip_when_ready(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exp DlReady")
        ids = [_seed_frame(pid, seq=i) for i in (1, 2)]
        csrf = csrf_of(operator_client, "/frames")
        operator_client.post(
            "/frames/export",
            data={"frame_ids": ",".join(str(i) for i in ids), "csrf_token": csrf},
            headers=_FORM,
        )
        job = _latest_export_job(pid)
        _mark_export_done_with_zip(job.id, pid)

        resp = operator_client.get(f"/frames/export/{job.id}/download")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/zip"
        assert f"export-{job.id}.zip" in resp.headers.get("content-disposition", "")
        assert resp.content.startswith(b"PK\x03\x04")

    def test_download_unknown_job_404(self, operator_client: TestClient) -> None:
        resp = operator_client.get("/frames/export/999999/download")
        assert resp.status_code == 404
