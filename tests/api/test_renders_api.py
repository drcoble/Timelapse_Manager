"""API-level tests for render and milestone endpoints."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Event, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.render.spec import project_render_root
from timelapse_manager.runtime import get_context
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

    @pytest.mark.parametrize(
        "range_header",
        [
            "bytes=" + "9" * 5000 + "-",
            "bytes=0-" + "9" * 5000,
            "bytes=-" + "9" * 5000,
            "bytes=" + "1" * 20 + "-",
            "bytes=abc-def",  # the existing malformed-range baseline
        ],
        ids=["huge-start", "huge-end", "huge-suffix", "20-digits", "malformed"],
    )
    def test_oversized_range_number_serves_the_full_file(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
        range_header: str,
    ) -> None:
        """A bound longer than 19 digits is an invalid range, handled exactly
        like any malformed range -- the app's parser declines it and Starlette's
        ``FileResponse`` answers (full 200, or 400/416 for a header it rejects)
        -- and never a 500 from ``int()`` on a multi-thousand-digit string."""
        content = b"\x01" * 1024
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        job_id, _ = _seed_done_render(
            migrated_factory, settings_no_autostart, project_id, content=content
        )

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/stream",
            headers={**_auth(cam_auth_token), "Range": range_header},
        )
        assert resp.status_code in (200, 400, 416)
        if resp.status_code == 200:
            assert resp.content == content

    @pytest.mark.parametrize(
        ("filename", "mime"),
        [
            ("render-ns.mp4", "video/mp4"),
            ("render-ns.webm", "video/webm"),
            ("render-ns.mkv", "video/x-matroska"),
        ],
    )
    def test_every_finished_render_streams_inline_with_range(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
        filename: str,
        mime: str,
    ) -> None:
        """#25: no server gate. A render with ``browser_streamable = false``
        (AV1/MP4, VP9/WebM, anything in MKV) is served like any other: 206 for a
        range, its real MIME type, and inline (no attachment disposition), so
        the browser can try to play it."""
        content = bytes(range(256)) * 8
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name=f"ns-{filename}"
        )
        job_id, _ = _seed_done_render(
            migrated_factory,
            settings_no_autostart,
            project_id,
            browser_streamable=False,
            content=content,
            filename=filename,
        )

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}/stream",
            headers={**_auth(cam_auth_token), "Range": "bytes=0-99"},
        )
        assert resp.status_code == 206
        assert resp.headers["content-type"].startswith(mime)
        assert resp.headers["content-range"] == f"bytes 0-99/{len(content)}"
        assert resp.content == content[:100]
        assert "attachment" not in resp.headers.get("content-disposition", "")

        full = migrated_client.get(
            f"/api/v1/renders/{job_id}/stream", headers=_auth(cam_auth_token)
        )
        assert full.status_code == 200
        assert full.headers["content-type"].startswith(mime)
        assert "attachment" not in full.headers.get("content-disposition", "")


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


# ---------------------------------------------------------------------------
# Test: cancel render (terminal short-circuit + unknown id)
# ---------------------------------------------------------------------------


class TestCancelRender:
    def test_cancel_terminal_render_returns_unchanged(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """A render already in a terminal state is returned unchanged.

        The terminal check runs before the worker is consulted, so a ``done`` job
        cancels cleanly (200, unchanged) with nothing in flight -- and the audit
        write is skipped.
        """
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="cancel-terminal"
        )
        job_id, _ = _seed_done_render(
            migrated_factory, settings_no_autostart, project_id
        )

        resp = migrated_client.post(
            f"/api/v1/renders/{job_id}/cancel",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == job_id
        assert body["status"] == "done"

    def test_cancel_unknown_render_returns_404(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
    ) -> None:
        """An unknown render id is a 404 (raised through the offloaded load)."""
        app = migrated_client.app
        _override_admin(app)
        resp = migrated_client.post(
            "/api/v1/renders/999999/cancel",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Test: delete render (issue #9)
# ---------------------------------------------------------------------------


def _seed_job_with_status(
    factory: sessionmaker,  # type: ignore[type-arg]
    project_id: int,
    status: str,
) -> int:
    with session_scope(factory) as session:
        job = RenderJob(project_id=project_id, kind="manual", status=status)
        session.add(job)
        session.flush()
        return job.id


def _render_exists(factory: sessionmaker, job_id: int) -> bool:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        return session.get(RenderJob, job_id) is not None


class _RecordingQueue:
    """Stands in for the render worker: records cancels and settles the job."""

    def __init__(self, factory: sessionmaker) -> None:  # type: ignore[type-arg]
        self._factory = factory
        self.cancelled: list[int] = []
        self.user_requested: list[bool] = []

    async def cancel_job(self, job_id: int, *, user_requested: bool = False) -> bool:
        self.cancelled.append(job_id)
        self.user_requested.append(user_requested)
        # Off the loop, like the real worker's settle write (the loop-DB guard
        # would otherwise flag this stand-in, not the route).
        await asyncio.to_thread(self._settle, job_id)
        return True

    def _settle(self, job_id: int) -> None:
        with session_scope(self._factory) as session:
            job = session.get(RenderJob, job_id)
            assert job is not None, "the row was deleted before the cancel"
            job.status = "failed"


class TestDeleteRender:
    def test_delete_done_render_returns_204_and_removes_file(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_admin(migrated_client.app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="delete-done"
        )
        job_id, output = _seed_done_render(
            migrated_factory, settings_no_autostart, project_id
        )

        resp = migrated_client.delete(
            f"/api/v1/renders/{job_id}", headers=_auth(cam_auth_token)
        )

        assert resp.status_code == 204
        assert not output.exists()
        assert not _render_exists(migrated_factory, job_id)
        assert (
            migrated_client.get(
                f"/api/v1/renders/{job_id}", headers=_auth(cam_auth_token)
            ).status_code
            == 404
        )

    def test_delete_unknown_render_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        _override_admin(migrated_client.app)
        resp = migrated_client.delete(
            "/api/v1/renders/999999", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 404

    def test_delete_returns_403_for_viewer(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_deny(migrated_client.app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="delete-403"
        )
        job_id, output = _seed_done_render(
            migrated_factory, settings_no_autostart, project_id
        )

        resp = migrated_client.delete(
            f"/api/v1/renders/{job_id}", headers=_auth(cam_auth_token)
        )

        assert resp.status_code == 403
        assert output.exists()
        assert _render_exists(migrated_factory, job_id)

    def test_delete_returns_401_without_token(
        self, migrated_client: TestClient
    ) -> None:
        assert migrated_client.delete("/api/v1/renders/1").status_code == 401

    def test_delete_missing_file_still_returns_204(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_operator(migrated_client.app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="delete-missing"
        )
        job_id, output = _seed_done_render(
            migrated_factory, settings_no_autostart, project_id
        )
        output.unlink()

        resp = migrated_client.delete(
            f"/api/v1/renders/{job_id}", headers=_auth(cam_auth_token)
        )

        assert resp.status_code == 204
        assert not _render_exists(migrated_factory, job_id)

    @pytest.mark.parametrize("job_status", ["pending", "encoding"])
    def test_running_render_is_cancelled_before_delete(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
        job_status: str,
    ) -> None:
        """Owner decision: delete on a running render cancels it first."""
        _override_admin(migrated_client.app)
        queue = _RecordingQueue(migrated_factory)
        get_context().render_queue = queue  # type: ignore[assignment]
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name=f"delete-{job_status}"
        )
        job_id = _seed_job_with_status(migrated_factory, project_id, job_status)

        resp = migrated_client.delete(
            f"/api/v1/renders/{job_id}", headers=_auth(cam_auth_token)
        )

        assert resp.status_code == 204
        assert queue.cancelled == [job_id]
        # A delete is a user's request: the worker must not log it as a failure.
        assert queue.user_requested == [True]
        assert not _render_exists(migrated_factory, job_id)

    def test_running_render_is_refused_without_a_worker(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """No worker to stop it: 503, and the row stays."""
        _override_admin(migrated_client.app)
        get_context().render_queue = None
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="delete-503"
        )
        job_id = _seed_job_with_status(migrated_factory, project_id, "encoding")

        resp = migrated_client.delete(
            f"/api/v1/renders/{job_id}", headers=_auth(cam_auth_token)
        )

        assert resp.status_code == 503
        assert _render_exists(migrated_factory, job_id)

    def test_render_that_will_not_stop_is_refused_with_409(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """A worker that cannot stop the job gets a 409, and the row stays."""
        _override_admin(migrated_client.app)
        get_context().render_queue = _StubbornQueue()  # type: ignore[assignment]
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="delete-409"
        )
        job_id = _seed_job_with_status(migrated_factory, project_id, "encoding")

        resp = migrated_client.delete(
            f"/api/v1/renders/{job_id}", headers=_auth(cam_auth_token)
        )

        assert resp.status_code == 409
        assert _render_exists(migrated_factory, job_id)

    def test_orphaned_encoding_render_is_deleted(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """An ``encoding`` row with no live task deletes without a restart (R1).

        It used to be refused with 409 until the service restarted: the worker's
        cancel touched only ``pending`` rows.
        """
        _override_admin(migrated_client.app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="delete-orphan"
        )
        # Encoding with no live task in the (never started) real worker.
        job_id = _seed_job_with_status(migrated_factory, project_id, "encoding")

        resp = migrated_client.delete(
            f"/api/v1/renders/{job_id}", headers=_auth(cam_auth_token)
        )

        assert resp.status_code == 204
        assert not _render_exists(migrated_factory, job_id)


class _StubbornQueue:
    """A worker whose cancel never changes the job."""

    async def cancel_job(self, job_id: int, *, user_requested: bool = False) -> bool:
        return False


# ---------------------------------------------------------------------------
# Test: update milestone (partial patch preserves the untouched position)
# ---------------------------------------------------------------------------


class TestUpdateMilestone:
    def test_update_label_only_preserves_position(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """A PATCH that sends only the label leaves the stored position untouched.

        Pins the partial-patch contract: the adapter applies exactly the fields
        the client sent, so a timestamp-only milestone keeps its timestamp (and
        its cleared frame index) when only the label is edited.
        """
        app = migrated_client.app
        _override_admin(app)
        project_id = _seed_project(
            migrated_factory, settings_no_autostart, name="ms-patch"
        )
        create = migrated_client.post(
            f"/api/v1/projects/{project_id}/milestones",
            json={"label": "orig", "position_timestamp": "2024-03-01T12:00:00Z"},
            headers=_auth(cam_auth_token),
        )
        assert create.status_code == 201
        ms_id = create.json()["id"]

        resp = migrated_client.patch(
            f"/api/v1/projects/{project_id}/milestones/{ms_id}",
            json={"label": "renamed"},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["label"] == "renamed"
        assert body["position_frame_index"] is None
        assert body["position_timestamp"] is not None


class TestParseRange:
    """``_parse_range`` never raises on an oversized bound (no ``int()`` of it)."""

    @pytest.mark.parametrize(
        "header",
        [
            "bytes=" + "9" * 5000 + "-",
            "bytes=0-" + "9" * 5000,
            "bytes=-" + "9" * 5000,
            "bytes=" + "1" * 20 + "-1",
        ],
        ids=["huge-start", "huge-end", "huge-suffix", "20-digits"],
    )
    def test_oversized_bound_is_invalid(self, header: str) -> None:
        from timelapse_manager.api.renders import _parse_range

        assert _parse_range(header, 1024) is None

    def test_nineteen_digits_still_parse(self) -> None:
        from timelapse_manager.api.renders import _parse_range

        assert _parse_range("bytes=0-" + "9" * 19, 1024) == (0, 1023)


# ---------------------------------------------------------------------------
# #67 R14: a repeated manual trigger returns the queued job
# ---------------------------------------------------------------------------


def _manual_job_ids(factory: sessionmaker, project_id: int) -> list[int]:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        return [
            job.id
            for job in session.query(RenderJob)
            .filter(RenderJob.project_id == project_id, RenderJob.kind == "manual")
            .order_by(RenderJob.id)
        ]


def _set_status(factory: sessionmaker, job_id: int, status: str) -> None:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        job.status = status


class TestTriggerDedupe:
    def _trigger(
        self, client: TestClient, token: str, project_id: int, body: dict
    ) -> object:
        return client.post(
            f"/api/v1/projects/{project_id}/renders",
            json=body,
            headers=_auth(token),
        )

    def test_repeat_while_queued_returns_the_same_job(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_admin(migrated_client.app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        first = self._trigger(migrated_client, cam_auth_token, project_id, {})
        second = self._trigger(migrated_client, cam_auth_token, project_id, {})

        assert first.status_code == second.status_code == 201  # type: ignore[attr-defined]
        assert "X-Render-Deduplicated" not in first.headers  # type: ignore[attr-defined]
        assert second.headers["X-Render-Deduplicated"] == "true"  # type: ignore[attr-defined]
        assert second.json()["id"] == first.json()["id"]  # type: ignore[attr-defined]
        assert _manual_job_ids(migrated_factory, project_id) == [first.json()["id"]]  # type: ignore[attr-defined]
        # The repeat writes no second "manual render N triggered" audit event.
        with session_scope(migrated_factory) as session:
            triggered = [
                e.message
                for e in session.query(Event).all()
                if e.message.endswith("triggered")
            ]
        assert triggered == [f"manual render {first.json()['id']} triggered"]  # type: ignore[attr-defined]

    def test_encoding_job_also_counts_as_queued(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """The worker may claim the first submit before the second arrives."""
        _override_admin(migrated_client.app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        first = self._trigger(migrated_client, cam_auth_token, project_id, {})
        _set_status(migrated_factory, first.json()["id"], "encoding")  # type: ignore[attr-defined]

        second = self._trigger(migrated_client, cam_auth_token, project_id, {})

        assert second.headers.get("X-Render-Deduplicated") == "true"  # type: ignore[attr-defined]
        assert second.json()["id"] == first.json()["id"]  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        "second_body",
        [
            {"output": {"width": 1280, "height": 720}},
            {"overlay": {"text_enabled": True, "text_content": "hello"}},
        ],
        ids=["other-output", "other-overlay"],
    )
    def test_different_settings_queue_a_new_job(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
        second_body: dict,
    ) -> None:
        _override_admin(migrated_client.app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        first = self._trigger(migrated_client, cam_auth_token, project_id, {})
        second = self._trigger(migrated_client, cam_auth_token, project_id, second_body)

        assert "X-Render-Deduplicated" not in second.headers  # type: ignore[attr-defined]
        assert second.json()["id"] != first.json()["id"]  # type: ignore[attr-defined]

    @pytest.mark.parametrize("status", ["done", "failed"])
    def test_finished_job_does_not_block_a_new_one(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
        status: str,
    ) -> None:
        _override_admin(migrated_client.app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        first = self._trigger(migrated_client, cam_auth_token, project_id, {})
        _set_status(migrated_factory, first.json()["id"], status)  # type: ignore[attr-defined]

        second = self._trigger(migrated_client, cam_auth_token, project_id, {})

        assert "X-Render-Deduplicated" not in second.headers  # type: ignore[attr-defined]
        assert second.json()["id"] != first.json()["id"]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# #67 S13: the render routes never serve an export
# ---------------------------------------------------------------------------


class TestExportIsNotARender:
    @pytest.mark.parametrize("route", ["", "/download", "/stream"])
    def test_export_id_is_404_on_render_routes(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
        route: str,
    ) -> None:
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        job_id, output = _seed_done_render(
            migrated_factory,
            settings_no_autostart,
            project_id,
            filename="export-1.zip",
        )
        with session_scope(migrated_factory) as session:
            job = session.get(RenderJob, job_id)
            assert job is not None
            job.kind = "export"

        resp = migrated_client.get(
            f"/api/v1/renders/{job_id}{route}", headers=_auth(cam_auth_token)
        )

        assert resp.status_code == 404
        assert output.exists()


# ---------------------------------------------------------------------------
# #67 R1 / O1: GET /api/v1/system/render-activity
# ---------------------------------------------------------------------------


class TestRenderActivity:
    def test_requires_the_bearer_token(self, migrated_client: TestClient) -> None:
        assert migrated_client.get("/api/v1/system/render-activity").status_code == 401

    def test_counts_rows_and_orphans_of_an_idle_worker(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        """The worker is not draining here: no live task, so encoding = orphan."""
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        for status in ("pending", "pending", "encoding", "done", "failed"):
            _seed_job_with_status(migrated_factory, project_id, status)

        resp = migrated_client.get(
            "/api/v1/system/render-activity", headers=_auth(cam_auth_token)
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "live_tasks": 0,
            "pending_rows": 2,
            "encoding_rows": 1,
            "orphans": 1,
        }

    def test_without_a_worker_counts_rows(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        get_context().render_queue = None
        project_id = _seed_project(migrated_factory, settings_no_autostart)
        for status in ("pending", "encoding", "encoding"):
            _seed_job_with_status(migrated_factory, project_id, status)

        resp = migrated_client.get(
            "/api/v1/system/render-activity", headers=_auth(cam_auth_token)
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "live_tasks": 0,
            "pending_rows": 1,
            "encoding_rows": 2,
            "orphans": 2,
        }

    def test_empty_queue_is_all_zero(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.get(
            "/api/v1/system/render-activity", headers=_auth(cam_auth_token)
        )

        assert resp.json() == {
            "live_tasks": 0,
            "pending_rows": 0,
            "encoding_rows": 0,
            "orphans": 0,
        }


# ---------------------------------------------------------------------------
# Overlay timestamp_format grammar (#67 S4)
# ---------------------------------------------------------------------------


class TestTriggerRenderTimestampFormat:
    @pytest.mark.parametrize(
        "fmt",
        [
            "'",
            "',null",
            "x',movie=/etc/passwd",
            "',drawtext=textfile=/etc/passwd",
            "[a];movie=x",
            "%{eif:1:d}",
            "\\",
            "%H\n",
            "",
            "a" * 65,
        ],
    )
    def test_hostile_format_returns_422_and_queues_nothing(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
        fmt: str,
    ) -> None:
        _override_admin(migrated_client.app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={"overlay": {"timestamp_enabled": True, "timestamp_format": fmt}},
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 422, resp.text
        assert "timestamp_format" in resp.text
        with session_scope(migrated_factory) as session:
            assert session.query(RenderJob).count() == 0

    def test_valid_format_is_accepted_and_stored(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_admin(migrated_client.app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={
                "overlay": {
                    "timestamp_enabled": True,
                    "timestamp_format": "%d/%m/%Y %H:%M",
                }
            },
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 201, resp.text
        with session_scope(migrated_factory) as session:
            job = session.get(RenderJob, resp.json()["id"])
            assert job is not None
            assert job.overlay_config is not None
            assert job.overlay_config["timestamp_format"] == "%d/%m/%Y %H:%M"


class TestTriggerRenderCaptionLength:
    """The overlay caption is capped so it fits an ffmpeg argument (#139)."""

    def test_caption_over_the_cap_is_422_and_queues_nothing(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        from timelapse_manager.encode.overlay import TEXT_CONTENT_MAX_LENGTH

        _override_admin(migrated_client.app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={
                "overlay": {
                    "text_enabled": True,
                    "text_content": "x" * (TEXT_CONTENT_MAX_LENGTH + 1),
                }
            },
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 422, resp.text
        assert "text_content" in resp.text
        with session_scope(migrated_factory) as session:
            assert session.query(RenderJob).count() == 0

    def test_caption_at_the_cap_is_accepted(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        from timelapse_manager.encode.overlay import TEXT_CONTENT_MAX_LENGTH

        _override_admin(migrated_client.app)
        project_id = _seed_project(migrated_factory, settings_no_autostart)

        resp = migrated_client.post(
            f"/api/v1/projects/{project_id}/renders",
            json={
                "overlay": {
                    "text_enabled": True,
                    "text_content": "x" * TEXT_CONTENT_MAX_LENGTH,
                }
            },
            headers=_auth(cam_auth_token),
        )

        assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Suggested render frame rate (#301)
# ---------------------------------------------------------------------------


def _add_frames(
    factory: sessionmaker,  # type: ignore[type-arg]
    project_id: int,
    interval_seconds: int,
    count: int,
) -> None:
    from datetime import timedelta

    from timelapse_manager.db.models import Frame

    first = datetime(2026, 1, 1, 12)
    with session_scope(factory) as session:
        for i in range(count):
            session.add(
                Frame(
                    project_id=project_id,
                    sequence_index=i,
                    capture_timestamp=first + timedelta(seconds=interval_seconds * i),
                    file_path=f"{i:08d}.jpg",
                    capture_status="captured",
                    origin="captured",
                    lifecycle_state="active",
                )
            )


class TestSuggestedFps:
    def test_requires_the_token(self, migrated_client: TestClient) -> None:
        resp = migrated_client.get("/api/v1/projects/1/renders/suggested-fps")
        assert resp.status_code == 401

    def test_returns_the_suggestion(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        _add_frames(migrated_factory, pid, 86400, 84)
        resp = migrated_client.get(
            f"/api/v1/projects/{pid}/renders/suggested-fps",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        assert resp.json() == {
            "fps": 12,
            "interval_seconds": 86400.0,
            "frame_count": 84,
            "expected_duration_seconds": 7.0,
        }

    def test_range_narrows_the_frames(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        _add_frames(migrated_factory, pid, 300, 100)
        resp = migrated_client.get(
            f"/api/v1/projects/{pid}/renders/suggested-fps",
            params={"start": "2026-01-01T12:00:00Z", "end": "2026-01-01T12:20:00Z"},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        assert resp.json()["frame_count"] == 5
        assert resp.json()["fps"] == 30

    def test_null_with_fewer_than_two_frames(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        _add_frames(migrated_factory, pid, 300, 1)
        body = migrated_client.get(
            f"/api/v1/projects/{pid}/renders/suggested-fps",
            headers=_auth(cam_auth_token),
        ).json()
        assert body == {
            "fps": None,
            "interval_seconds": None,
            "frame_count": 1,
            "expected_duration_seconds": None,
        }

    def test_unknown_project_and_bad_range(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        pid = _seed_project(migrated_factory, settings_no_autostart)
        missing = migrated_client.get(
            "/api/v1/projects/99999/renders/suggested-fps",
            headers=_auth(cam_auth_token),
        )
        assert missing.status_code == 404
        backwards = migrated_client.get(
            f"/api/v1/projects/{pid}/renders/suggested-fps",
            params={"start": "2026-01-02T00:00:00Z", "end": "2026-01-01T00:00:00"},
            headers=_auth(cam_auth_token),
        )
        assert backwards.status_code == 422

    def test_trigger_without_fps_keeps_the_default(
        self,
        migrated_client: TestClient,
        migrated_factory: sessionmaker,  # type: ignore[type-arg]
        settings_no_autostart: Settings,
        cam_auth_token: str,
    ) -> None:
        _override_admin(migrated_client.app)
        pid = _seed_project(migrated_factory, settings_no_autostart)
        _add_frames(migrated_factory, pid, 86400, 30)
        resp = migrated_client.post(
            f"/api/v1/projects/{pid}/renders",
            json={},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201
        with session_scope(migrated_factory) as session:
            job = session.get(RenderJob, resp.json()["id"])
            assert job is not None
            assert job.output_settings["fps"] == (
                settings_no_autostart.render.default_fps
            )
