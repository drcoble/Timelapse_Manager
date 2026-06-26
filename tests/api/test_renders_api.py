"""API-level tests for render and milestone endpoints."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.render.spec import project_render_root
from timelapse_manager.security.principal import (
    Principal,
    require_operator_or_admin_principal,
)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _override_admin(app: object) -> None:
    """Override the mutation principal to return a real admin principal."""
    app.dependency_overrides[require_operator_or_admin_principal] = lambda: Principal(  # type: ignore[attr-defined]
        user_id=1, role="admin"
    )


def _override_operator(app: object) -> None:
    """Override the mutation principal to return a real operator principal."""
    app.dependency_overrides[require_operator_or_admin_principal] = lambda: Principal(  # type: ignore[attr-defined]
        user_id=1, role="operator"
    )


def _override_deny(app: object) -> None:
    """Override the mutation principal to raise 403."""
    from fastapi import HTTPException

    def _deny() -> Principal:
        raise HTTPException(status_code=403, detail="forbidden")

    app.dependency_overrides[require_operator_or_admin_principal] = _deny  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# DB seed helpers
# ---------------------------------------------------------------------------


def _seed_project(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    *,
    name: str = "api-project",
) -> int:
    frames_root = settings.paths.frames_root
    assert frames_root is not None
    with session_scope(factory) as session:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

        frame_dir = frames_root / str(project_id)
        frame_dir.mkdir(parents=True, exist_ok=True)

    return project_id


def _seed_done_render(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    project_id: int,
    *,
    browser_streamable: bool = True,
    content: bytes = b"\x00" * 2048,
    filename: str = "render-1.mp4",
) -> tuple[int, Path]:
    """Insert a done RenderJob and place a real output file in the render root."""
    with session_scope(factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings, proj)
        render_root.mkdir(parents=True, exist_ok=True)
        output = render_root / filename
        output.write_bytes(content)

        job = RenderJob(
            project_id=project_id,
            kind="manual",
            status="done",
            output_settings={
                "fps": 1.0,
                "width": 64,
                "height": 48,
                "codec": "h264",
                "container": "mp4",
            },
            output_file_path=str(output),
            browser_streamable=browser_streamable,
            completed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(job)
        session.flush()
        job_id = job.id

    return job_id, output


# ---------------------------------------------------------------------------
# Test: 401 without token
# ---------------------------------------------------------------------------


class TestAuth:
    def test_trigger_render_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        resp = migrated_client.post("/api/v1/projects/1/renders", json={})
        assert resp.status_code == 401

    def test_list_renders_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        resp = migrated_client.get("/api/v1/projects/1/renders")
        assert resp.status_code == 401

    def test_get_render_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        resp = migrated_client.get("/api/v1/renders/1")
        assert resp.status_code == 401

    def test_download_render_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        resp = migrated_client.get("/api/v1/renders/1/download")
        assert resp.status_code == 401

    def test_stream_render_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        resp = migrated_client.get("/api/v1/renders/1/stream")
        assert resp.status_code == 401

    def test_create_milestone_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        resp = migrated_client.post(
            "/api/v1/projects/1/milestones",
            json={"label": "x", "position_frame_index": 0},
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Test: 403 admin gate on mutations
# ---------------------------------------------------------------------------


class TestAdminGate:
    def test_trigger_render_returns_403_for_non_admin(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_deny(app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 403

    def test_create_milestone_returns_403_for_non_admin(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_deny(app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/milestones",
            json={"label": "x", "position_frame_index": 0},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 403

    def test_trigger_render_allowed_for_operator(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        # An operator principal is admitted on the render mutation surface.
        app = migrated_client.app
        _override_operator(app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201


# ---------------------------------------------------------------------------
# Test: trigger render → 201 pending
# ---------------------------------------------------------------------------


class TestTriggerRender:
    def test_trigger_returns_201_with_pending_status(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={},
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 201
        body = resp.json()
        assert body["status"] == "pending"
        assert body["project_id"] == project_id
        assert body["kind"] == "manual"

    def test_trigger_unknown_project_returns_404(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_admin(app)

        resp = migrated_client.post(
            "/api/v1/projects/99999/renders",
            json={},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404

    def test_trigger_unsupported_codec_returns_400(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={"output": {"codec": "mpeg4", "container": "mp4"}},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Test: download → 200 video/mp4
# ---------------------------------------------------------------------------


class TestDownloadRender:
    def test_download_done_render_returns_200(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        job_id, _ = _seed_done_render(
            migrated_factory, settings_no_autostart, project_id
        )

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/download",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        assert "video/mp4" in resp.headers.get("content-type", "")

    def test_download_pending_render_returns_404(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        with session_scope(migrated_factory) as session:
            job = RenderJob(
                project_id=project_id,
                kind="manual",
                status="pending",
                output_settings={},
            )
            session.add(job)
            session.flush()
            job_id = job.id

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/download",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: stream with Range header → 206 + correct headers (streamable only)
# ---------------------------------------------------------------------------


class TestStreamRender:
    def test_stream_without_range_returns_200(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        job_id, _ = _seed_done_render(
            migrated_factory,
            settings_no_autostart,
            project_id,
            browser_streamable=True,
            content=b"\x00" * 1024,
        )

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/stream",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200

    def test_stream_with_valid_range_returns_206(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        content = b"\x00" * 2048
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        job_id, _ = _seed_done_render(
            migrated_factory,
            settings_no_autostart,
            project_id,
            browser_streamable=True,
            content=content,
        )

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/stream",
            headers={**_auth(cam_auth_token), "Range": "bytes=0-511"},
        )
        assert resp.status_code == 206
        assert "Content-Range" in resp.headers
        assert resp.headers["Content-Range"] == "bytes 0-511/2048"
        assert resp.headers["Content-Length"] == "512"

    def test_stream_range_content_length_is_correct(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        content = bytes(range(256)) * 8  # 2048 bytes
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        job_id, _ = _seed_done_render(
            migrated_factory,
            settings_no_autostart,
            project_id,
            browser_streamable=True,
            content=content,
        )

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/stream",
            headers={**_auth(cam_auth_token), "Range": "bytes=100-199"},
        )
        assert resp.status_code == 206
        assert len(resp.content) == 100

    def test_stream_range_past_eof_is_clamped_to_file_end(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        content = b"\x00" * 100
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        job_id, _ = _seed_done_render(
            migrated_factory,
            settings_no_autostart,
            project_id,
            browser_streamable=True,
            content=content,
        )

        # Request past EOF — end should be clamped to 99.
        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/stream",
            headers={**_auth(cam_auth_token), "Range": "bytes=0-9999"},
        )
        assert resp.status_code == 206
        assert "bytes 0-99/100" in resp.headers.get("Content-Range", "")

    def test_stream_range_start_at_eof_is_unsatisfiable(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """A range starting at/past EOF causes _parse_range to return None.

        The app falls back to FileResponse(path). Starlette's FileResponse
        then processes the Range header at the ASGI layer and may return 416
        or 200 depending on version. The invariant: it is not our 206 StreamingResponse.
        """
        content = b"\x00" * 100
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="eof-proj"
        )
        job_id, _ = _seed_done_render(
            migrated_factory,
            settings_no_autostart,
            project_id,
            browser_streamable=True,
            content=content,
        )

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/stream",
            headers={**_auth(cam_auth_token), "Range": "bytes=100-200"},
        )
        # _parse_range returns None for start >= file_size → FileResponse fallback.
        # Not our StreamingResponse — no Content-Range with our format.
        assert resp.status_code in (200, 416)

    def test_non_streamable_render_without_range_returns_200(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """Non-streamable render served without a Range header returns 200."""
        content = b"\x00" * 1024
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="ns-proj"
        )
        job_id, _ = _seed_done_render(
            migrated_factory,
            settings_no_autostart,
            project_id,
            browser_streamable=False,
            content=content,
            filename="render-ns.mkv",
        )

        # No Range header: non-streamable path returns FileResponse(200).
        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/stream",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200

    def test_non_streamable_render_with_valid_range_handled_by_starlette(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """Starlette's FileResponse honors Range for non-streamable renders.

        The application skips its custom range logic and returns FileResponse.
        Starlette processes the Range header at the ASGI level, returning 206.
        """
        content = b"\x00" * 2048
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="ns-range-proj"
        )
        job_id, _ = _seed_done_render(
            migrated_factory,
            settings_no_autostart,
            project_id,
            browser_streamable=False,
            content=content,
            filename="render-ns2.mkv",
        )

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/stream",
            headers={**_auth(cam_auth_token), "Range": "bytes=0-99"},
        )
        # Starlette FileResponse processes Range at ASGI level → 206.
        assert resp.status_code == 206


# ---------------------------------------------------------------------------
# Test: milestones CRUD
# ---------------------------------------------------------------------------


class TestMilestones:
    def test_create_milestone_with_frame_index_returns_201(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="ms-proj-1"
        )

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/milestones",
            json={"label": "Week 1", "position_frame_index": 0},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["label"] == "Week 1"
        assert body["position_frame_index"] == 0
        assert body["project_id"] == project_id

    def test_create_milestone_with_timestamp_returns_201(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="ms-proj-ts"
        )

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/milestones",
            json={
                "label": "Phase 2",
                "position_timestamp": "2024-03-15T00:00:00Z",
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201

    def test_create_milestone_with_no_position_returns_422(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="ms-proj-422"
        )

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/milestones",
            json={"label": "no position"},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422

    def test_list_milestones_returns_empty_initially(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="ms-proj-list"
        )

        resp = migrated_client.get(
            f"/api/v1/projects/{project_id}/milestones",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_milestones_after_create_includes_milestone(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="ms-proj-list2"
        )

        migrated_client.post(
            f"/api/v1/projects/{project_id}/milestones",
            json={"label": "Marker", "position_frame_index": 5},
            headers=_auth(cam_auth_token),
        )

        resp = migrated_client.get(
            f"/api/v1/projects/{project_id}/milestones",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        milestones = resp.json()
        assert len(milestones) == 1
        assert milestones[0]["label"] == "Marker"

    def test_delete_milestone_returns_204(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="ms-proj-del"
        )

        create_resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/milestones",
            json={"label": "To Delete", "position_frame_index": 0},
            headers=_auth(cam_auth_token),
        )
        milestone_id = create_resp.json()["id"]

        del_resp = migrated_client.delete(
            f"/api/v1/projects/{project_id}/milestones/{milestone_id}",
            headers=_auth(cam_auth_token),
        )
        assert del_resp.status_code == 204

        # Verify it's gone.
        list_resp = migrated_client.get(
            f"/api/v1/projects/{project_id}/milestones",
            headers=_auth(cam_auth_token),
        )
        assert list_resp.json() == []

    def test_delete_nonexistent_milestone_returns_404(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="ms-proj-404"
        )

        resp = migrated_client.delete(
            f"/api/v1/projects/{project_id}/milestones/99999",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: list renders
# ---------------------------------------------------------------------------


class TestListRenders:
    def test_list_renders_returns_empty_initially(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="list-proj"
        )

        resp = migrated_client.get(
            f"/api/v1/projects/{project_id}/renders",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_list_renders_after_trigger_includes_job(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="list-proj-2"
        )

        migrated_client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={},
            headers=_auth(cam_auth_token),
        )

        resp = migrated_client.get(
            f"/api/v1/projects/{project_id}/renders",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        renders = resp.json()
        assert len(renders) == 1
        assert renders[0]["status"] == "pending"


# ---------------------------------------------------------------------------
# Test: get single render
# ---------------------------------------------------------------------------


class TestGetRender:
    def test_get_render_returns_correct_fields(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="get-render-proj"
        )
        job_id, _ = _seed_done_render(
            migrated_factory, settings_no_autostart, project_id
        )

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == job_id
        assert body["project_id"] == project_id
        assert body["status"] == "done"

    def test_get_nonexistent_render_returns_404(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
    ) -> None:
        resp = migrated_client.get(
            "/api/v1/renders/99999",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404
