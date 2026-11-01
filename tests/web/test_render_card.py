"""Web tests for the render card, the export card, and deleting from them (#9).

* ``GET /renders/{id}/card`` -- one fragment for both render lists; per status
  (pending/encoding/done/failed) and kind (manual/scheduled/archive); failure
  reason, "file missing", triggered-by, archive note; role-gated actions.
* ``GET /frames/export/{id}/card`` -- the export card on the Frames tab.
* ``POST /renders/{id}/delete`` and ``POST /frames/export/{id}/delete`` -- CSRF,
  roles, the HTMX answer (row removal, toast, dialog close) and the redirect.
* The render lists leave exports out; the project Frames tab lists them.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Event, Project, RenderJob, User
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring.events import EventType, log_event
from timelapse_manager.render.spec import project_render_root
from timelapse_manager.runtime import get_context
from timelapse_manager.security.token import ensure_local_token

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_HTMX = {"HX-Request": "true"}


def _seed_project(name: str = "Card Project") -> int:
    with session_scope(get_context().session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="10.0.0.5", protocol="vapix")
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


def _render_root(project_id: int) -> Path:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        project = db.get(Project, project_id)
        assert project is not None
        root = project_render_root(ctx.settings, project)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _seed_job(
    project_id: int,
    *,
    kind: str = "manual",
    status: str = "done",
    with_file: bool = True,
    **fields: object,
) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        job = RenderJob(
            project_id=project_id,
            kind=kind,
            status=status,
            output_settings={
                "fps": 24,
                "width": 1920,
                "height": 1080,
                "codec": "h264",
                "container": "mp4",
            },
            browser_streamable=True,
            started_at=datetime(2026, 10, 1, 12, 0, 0),
            completed_at=(
                datetime(2026, 10, 1, 12, 3, 30)
                if status in ("done", "failed")
                else None
            ),
            **fields,
        )
        db.add(job)
        db.flush()
        job_id = job.id
    if with_file and status == "done":
        suffix = "zip" if kind == "export" else "mp4"
        name = f"{'export' if kind == 'export' else 'render'}-{job_id}.{suffix}"
        path = _render_root(project_id) / name
        path.write_bytes(b"\x00" * 2048)
        with session_scope(ctx.session_factory) as db:
            row = db.get(RenderJob, job_id)
            assert row is not None
            row.output_file_path = str(path)
    return job_id


def _job_exists(job_id: int) -> bool:
    with session_scope(get_context().session_factory) as db:
        return db.get(RenderJob, job_id) is not None


def _output_path(job_id: int) -> Path:
    with session_scope(get_context().session_factory) as db:
        row = db.get(RenderJob, job_id)
        assert row is not None and row.output_file_path
        return Path(row.output_file_path)


def _audit_trigger(project_id: int, message: str, username: str) -> None:
    with session_scope(get_context().session_factory) as db:
        user = db.execute(select(User).where(User.username == username)).scalar_one()
        db.add(
            Event(
                scope="project",
                scope_id=project_id,
                level="info",
                message=message,
                actor_user_id=user.id,
            )
        )


def _card(client: TestClient, url: str) -> str:
    resp = client.get(url, headers=_HTMX)
    assert resp.status_code == 200, resp.text[:300]
    return resp.text


# ---------------------------------------------------------------------------
# Render card
# ---------------------------------------------------------------------------


class TestRenderCard:
    def test_done_manual_render_shows_every_field(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project("Garden")
        job_id = _seed_job(
            pid,
            frame_count=480,
            first_frame_at=datetime(2026, 9, 1, 6, 0),
            last_frame_at=datetime(2026, 9, 30, 18, 0),
            chapters=[
                {"timecode_seconds": 0.0, "label": "September"},
                {"timecode_seconds": 10.0, "label": "Week 2"},
            ],
            overlay_config={"timestamp_enabled": True, "text_enabled": False},
        )
        _audit_trigger(pid, f"manual render {job_id} triggered", "admin")

        html = _card(admin_client, f"/renders/{job_id}/card")

        assert f"Render #{job_id}" in html
        assert 'class="kind-badge manual"' in html
        assert "Complete" in html
        assert f'href="/projects/{pid}"' in html and "Garden" in html
        assert "admin" in html  # triggered by
        assert "3m 30s" in html  # how long it took
        assert "h264" in html and "mp4" in html and "1920×1080" in html
        assert "24 fps" in html
        assert "480 frames" in html
        assert "20.0s" in html  # 480 frames at 24 fps
        assert "2 chapters" in html and "Week 2" in html and "0:10" in html
        assert "Timestamp" in html  # overlay
        assert f"render-{job_id}.mp4" in html and "2.0 KB" in html
        assert f'src="/renders/{job_id}/stream"' in html
        assert f'href="/renders/{job_id}/download"' in html
        assert "File missing" not in html

    def test_old_render_says_not_recorded(self, admin_client: TestClient) -> None:
        """A job from before the frame window was stored shows "not recorded"."""
        pid = _seed_project()
        job_id = _seed_job(pid)

        html = _card(admin_client, f"/renders/{job_id}/card")

        assert html.count("not recorded") >= 3  # frames, length, chapters, by

    @pytest.mark.parametrize(
        ("status", "label"),
        [("pending", "Queued"), ("encoding", "Running")],
    )
    def test_running_render_offers_cancel_and_warns_on_delete(
        self, admin_client: TestClient, status: str, label: str
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, status=status)

        html = _card(admin_client, f"/renders/{job_id}/card")

        assert label in html
        assert f'hx-post="/renders/{job_id}/cancel"' in html
        assert "it will be stopped first" in html
        assert f'hx-post="/renders/{job_id}/delete"' in html
        assert "Download" not in html

    def test_failed_render_shows_failure_reason(self, admin_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, status="failed")
        other_id = _seed_job(pid, status="failed")
        with session_scope(get_context().session_factory) as db:
            log_event(
                db,
                scope="project",
                scope_id=pid,
                level="error",
                type=EventType.RENDER_FAILED.value,
                message=f"render {job_id} failed: ffmpeg exited with status 1",
                metadata={"render_id": job_id},
            )
            log_event(
                db,
                scope="project",
                scope_id=pid,
                level="error",
                type=EventType.RENDER_FAILED.value,
                message=f"render {other_id} failed: someone else's reason",
                metadata={"render_id": other_id},
            )

        html = _card(admin_client, f"/renders/{job_id}/card")

        assert "Error" in html
        assert "Failure reason" in html
        assert "ffmpeg exited with status 1" in html
        assert f"render {job_id} failed:" not in html  # prefix stripped
        assert "someone else" not in html

    def test_new_render_after_delete_shows_none_of_the_old_ones_history(
        self, admin_client: TestClient
    ) -> None:
        """A deleted failed render's reason and trigger never reach a new job."""
        pid = _seed_project()
        old_id = _seed_job(pid, status="failed")
        _audit_trigger(pid, f"manual render {old_id} triggered", "admin")
        with session_scope(get_context().session_factory) as db:
            log_event(
                db,
                scope="project",
                scope_id=pid,
                level="error",
                type=EventType.RENDER_FAILED.value,
                message=f"render {old_id} failed: the old job's reason",
                metadata={"render_id": old_id},
            )
        csrf = csrf_of(admin_client, "/renders")
        admin_client.post(
            f"/renders/{old_id}/delete", data={"csrf_token": csrf}, headers=_FORM
        )

        new_id = _seed_job(pid, status="failed")
        html = _card(admin_client, f"/renders/{new_id}/card")

        assert new_id > old_id
        assert "the old job's reason" not in html
        assert "Triggered by" in html
        assert html.count("not recorded") >= 2  # failure reason and trigger

    @pytest.mark.parametrize("kind", ["manual", "export"])
    def test_events_older_than_the_job_are_ignored(
        self, admin_client: TestClient, kind: str
    ) -> None:
        """Belt and braces for ids reused before migration 028.

        The events name the same id as the job but were written before the job
        existed, as happens when an older, deleted job's id was handed out
        again on a database that predates AUTOINCREMENT.
        """
        pid = _seed_project()
        job_id = _seed_job(pid, kind=kind, status="failed")
        trigger = (
            f"export {job_id} triggered"
            if kind == "export"
            else f"manual render {job_id} triggered"
        )
        long_ago = datetime(2020, 1, 1)
        with session_scope(get_context().session_factory) as db:
            admin = db.execute(
                select(User).where(User.username == "admin")
            ).scalar_one()
            db.add(
                Event(
                    scope="project",
                    scope_id=pid,
                    level="info",
                    message=trigger,
                    actor_user_id=admin.id,
                    timestamp=long_ago,
                )
            )
            db.add(
                Event(
                    scope="project",
                    scope_id=pid,
                    level="error",
                    message=f"render {job_id} failed: a stale reason",
                    event_metadata={
                        "render_id": job_id,
                        "type": EventType.RENDER_FAILED.value,
                    },
                    timestamp=long_ago,
                )
            )

        url = (
            f"/frames/export/{job_id}/card"
            if kind == "export"
            else f"/renders/{job_id}/card"
        )
        html = _card(admin_client, url)

        assert "a stale reason" not in html
        assert ">admin<" not in html
        assert html.count("not recorded") >= 2

    def test_done_render_with_missing_file_warns(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid)
        _output_path(job_id).unlink()

        html = _card(admin_client, f"/renders/{job_id}/card")

        assert "File missing" in html
        assert f'href="/renders/{job_id}/download"' not in html
        assert "/stream" not in html
        assert "only the" in html and "record will be removed" in html

    @pytest.mark.parametrize("kind", ["scheduled", "archive"])
    def test_scheduled_and_archive_renders_are_by_schedule(
        self, admin_client: TestClient, kind: str
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, kind=kind)

        html = _card(admin_client, f"/renders/{job_id}/card")

        assert f'class="kind-badge {kind}"' in html
        assert "schedule" in html
        if kind == "archive":
            assert "automatic pruning keeps archive renders" in html
            assert "This is an archive render" in html  # delete warning
        else:
            assert "archive render" not in html.lower()

    def test_viewer_can_open_play_download_but_not_cancel_or_delete(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        done_id = _seed_job(pid)
        running_id = _seed_job(pid, status="encoding")

        done_html = _card(viewer_client, f"/renders/{done_id}/card")
        running_html = _card(viewer_client, f"/renders/{running_id}/card")

        assert f'href="/renders/{done_id}/download"' in done_html
        assert f'src="/renders/{done_id}/stream"' in done_html
        for html in (done_html, running_html):
            assert "/delete" not in html
            assert "/cancel" not in html
            assert "btn-danger" not in html

    def test_export_job_is_not_a_render_card(self, admin_client: TestClient) -> None:
        pid = _seed_project()
        export_id = _seed_job(pid, kind="export")
        resp = admin_client.get(f"/renders/{export_id}/card", headers=_HTMX)
        assert resp.status_code == 404

    def test_unknown_render_is_404(self, admin_client: TestClient) -> None:
        assert admin_client.get("/renders/9999/card", headers=_HTMX).status_code == 404

    def test_plain_request_gets_a_full_page(self, admin_client: TestClient) -> None:
        """Without JavaScript the row's link opens the card as a page."""
        pid = _seed_project()
        job_id = _seed_job(pid)

        resp = admin_client.get(f"/renders/{job_id}/card")

        assert resp.status_code == 200
        assert "<html" in resp.text and "job-card-page" in resp.text
        assert f"Render #{job_id}" in resp.text


# ---------------------------------------------------------------------------
# Export card
# ---------------------------------------------------------------------------


class TestExportCard:
    def test_done_export_shows_details(self, admin_client: TestClient) -> None:
        pid = _seed_project("Exports Project")
        job_id = _seed_job(
            pid,
            kind="export",
            frame_count=12,
            first_frame_at=datetime(2026, 9, 1, 6, 0),
            last_frame_at=datetime(2026, 9, 2, 6, 0),
        )
        _audit_trigger(pid, f"export {job_id} triggered", "admin")

        html = _card(admin_client, f"/frames/export/{job_id}/card")

        assert f"Export #{job_id}" in html
        assert "Ready" in html or "Complete" in html
        assert "Started by" in html and "admin" in html
        assert "3m 30s" in html
        assert "12 frames in the zip" in html
        assert f"export-{job_id}.zip" in html and "2.0 KB" in html
        assert f'href="/frames/export/{job_id}/download"' in html
        assert f'hx-post="/frames/export/{job_id}/delete"' in html
        assert "/stream" not in html

    def test_failed_export_shows_reason(self, admin_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, kind="export", status="failed")
        with session_scope(get_context().session_factory) as db:
            log_event(
                db,
                scope="project",
                scope_id=pid,
                level="error",
                type=EventType.RENDER_FAILED.value,
                message=f"render {job_id} failed: insufficient free disk space",
                metadata={"render_id": job_id},
            )

        html = _card(admin_client, f"/frames/export/{job_id}/card")

        assert "insufficient free disk space" in html

    def test_missing_zip_warns(self, admin_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, kind="export")
        _output_path(job_id).unlink()

        html = _card(admin_client, f"/frames/export/{job_id}/card")

        assert "File missing" in html
        assert "/download" not in html

    @pytest.mark.parametrize("status", ["pending", "encoding"])
    def test_running_export_warns_it_will_be_stopped(
        self, admin_client: TestClient, status: str
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, kind="export", status=status)

        html = _card(admin_client, f"/frames/export/{job_id}/card")

        assert "it will be stopped first" in html

    def test_viewer_sees_details_only(self, viewer_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, kind="export")

        html = _card(viewer_client, f"/frames/export/{job_id}/card")

        assert f"Export #{job_id}" in html
        assert "/download" not in html
        assert "/delete" not in html

    def test_render_job_is_not_an_export_card(self, admin_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid)
        resp = admin_client.get(f"/frames/export/{job_id}/card", headers=_HTMX)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Delete from the card
# ---------------------------------------------------------------------------


class TestDeleteRoutes:
    def test_htmx_delete_removes_row_toasts_and_closes_dialog(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid)
        output = _output_path(job_id)
        csrf = csrf_of(admin_client, "/renders")

        resp = admin_client.post(
            f"/renders/{job_id}/delete",
            data={"csrf_token": csrf},
            headers={**_FORM, **_HTMX},
        )

        assert resp.status_code == 200
        assert resp.headers.get("HX-Trigger") == "modal:close"
        assert f'id="render-row-{job_id}" hx-swap-oob="delete"' in resp.text
        assert 'hx-swap-oob="beforeend:#toast-region"' in resp.text
        assert f"Render #{job_id} deleted" in resp.text
        assert not output.exists()
        assert not _job_exists(job_id)
        with session_scope(get_context().session_factory) as db:
            messages = db.execute(select(Event.message)).scalars().all()
        assert any(
            m.startswith(f"render {job_id} deleted by admin (file render-{job_id}.mp4")
            for m in messages
        )

    def test_plain_post_redirects_back(self, operator_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid)
        csrf = csrf_of(operator_client, "/renders")

        resp = operator_client.post(
            f"/renders/{job_id}/delete",
            data={"csrf_token": csrf, "next": f"/projects/{pid}"},
            headers=_FORM,
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/projects/{pid}"
        assert not _job_exists(job_id)

    @pytest.mark.parametrize(
        "next_url", ["https://evil.example/", "//evil.example", "/users", ""]
    )
    def test_unsafe_next_falls_back_to_the_list(
        self, admin_client: TestClient, next_url: str
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid)
        csrf = csrf_of(admin_client, "/renders")

        resp = admin_client.post(
            f"/renders/{job_id}/delete",
            data={"csrf_token": csrf, "next": next_url},
            headers=_FORM,
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == "/renders"

    def test_delete_without_csrf_is_rejected(self, admin_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid)

        resp = admin_client.post(
            f"/renders/{job_id}/delete", headers=_FORM, follow_redirects=False
        )

        assert resp.status_code == 403
        assert _job_exists(job_id)

    def test_viewer_cannot_delete(self, viewer_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid)
        csrf = csrf_of(viewer_client, "/renders")

        resp = viewer_client.post(
            f"/renders/{job_id}/delete",
            data={"csrf_token": csrf},
            headers=_FORM,
            follow_redirects=False,
        )

        assert resp.status_code == 403
        assert _job_exists(job_id)

    def test_unknown_render_htmx_delete_shows_error_toast(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/renders")
        resp = admin_client.post(
            "/renders/9999/delete",
            data={"csrf_token": csrf},
            headers={**_FORM, **_HTMX},
        )
        assert resp.status_code == 200
        assert "toast error" in resp.text
        assert "HX-Trigger" not in resp.headers

    def test_unknown_render_plain_delete_is_404(self, admin_client: TestClient) -> None:
        csrf = csrf_of(admin_client, "/renders")
        resp = admin_client.post(
            "/renders/9999/delete",
            data={"csrf_token": csrf},
            headers=_FORM,
            follow_redirects=False,
        )
        assert resp.status_code == 404

    def test_render_delete_route_refuses_an_export(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        export_id = _seed_job(pid, kind="export")
        csrf = csrf_of(admin_client, "/renders")
        resp = admin_client.post(
            f"/renders/{export_id}/delete",
            data={"csrf_token": csrf},
            headers=_FORM,
            follow_redirects=False,
        )
        assert resp.status_code == 404
        assert _job_exists(export_id)

    def test_pending_render_is_cancelled_then_deleted(
        self, admin_client: TestClient
    ) -> None:
        """The (not draining) worker flips the queued job failed; then it goes."""
        pid = _seed_project()
        job_id = _seed_job(pid, status="pending")
        csrf = csrf_of(admin_client, "/renders")

        resp = admin_client.post(
            f"/renders/{job_id}/delete",
            data={"csrf_token": csrf},
            headers={**_FORM, **_HTMX},
        )

        assert resp.status_code == 200
        assert f"Render #{job_id} deleted" in resp.text
        assert not _job_exists(job_id)

    def test_export_delete_removes_zip_row_and_audits(
        self, operator_client: TestClient
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, kind="export")
        output = _output_path(job_id)
        csrf = csrf_of(operator_client, f"/projects/{pid}")

        resp = operator_client.post(
            f"/frames/export/{job_id}/delete",
            data={"csrf_token": csrf},
            headers={**_FORM, **_HTMX},
        )

        assert resp.status_code == 200
        assert f'id="export-row-{job_id}" hx-swap-oob="delete"' in resp.text
        assert f"Export #{job_id} deleted" in resp.text
        assert not output.exists()
        assert not _job_exists(job_id)
        with session_scope(get_context().session_factory) as db:
            messages = db.execute(select(Event.message)).scalars().all()
        assert any(
            m.startswith(f"export {job_id} deleted by operator") for m in messages
        )

    def test_export_plain_delete_redirects_to_project(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, kind="export")
        csrf = csrf_of(admin_client, f"/projects/{pid}")

        resp = admin_client.post(
            f"/frames/export/{job_id}/delete",
            data={"csrf_token": csrf},
            headers=_FORM,
            follow_redirects=False,
        )

        assert resp.status_code == 303
        assert resp.headers["location"] == f"/projects/{pid}"

    def test_export_delete_route_refuses_a_render(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid)
        csrf = csrf_of(admin_client, f"/projects/{pid}")
        resp = admin_client.post(
            f"/frames/export/{job_id}/delete",
            data={"csrf_token": csrf},
            headers=_FORM,
            follow_redirects=False,
        )
        assert resp.status_code == 404
        assert _job_exists(job_id)


class TestCancelRoute:
    def test_cancel_from_card_settles_job_and_reshows_card(
        self, operator_client: TestClient
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, status="pending")
        csrf = csrf_of(operator_client, "/renders")

        resp = operator_client.post(
            f"/renders/{job_id}/cancel",
            data={"csrf_token": csrf},
            headers={**_FORM, **_HTMX},
        )

        assert resp.status_code == 200
        assert f"Render #{job_id}" in resp.text and "Error" in resp.text
        assert f"/renders/{job_id}/cancel" not in resp.text
        with session_scope(get_context().session_factory) as db:
            row = db.get(RenderJob, job_id)
            assert row is not None and row.status == "failed"
        # The card's reason says who stopped it, not "not recorded".
        assert "cancelled by operator" in resp.text

    def test_user_cancel_is_info_and_raises_no_alert(
        self, operator_client: TestClient
    ) -> None:
        """A user's cancel is recorded at info, not as a render failure."""
        from timelapse_manager.monitoring.alerts import get_active_alerts

        pid = _seed_project()
        job_id = _seed_job(pid, status="pending")
        csrf = csrf_of(operator_client, "/renders")

        operator_client.post(
            f"/renders/{job_id}/cancel",
            data={"csrf_token": csrf},
            headers={**_FORM, **_HTMX},
        )

        with session_scope(get_context().session_factory) as db:
            events = [
                e
                for e in db.execute(select(Event)).scalars()
                if (e.event_metadata or {}).get("render_id") == job_id
            ]
            assert [(e.level, e.event_metadata["type"], e.message) for e in events] == [
                (
                    "info",
                    EventType.RENDER_CANCELLED.value,
                    f"render {job_id} cancelled by operator",
                )
            ]
            assert events[0].actor_user_id is not None
            alerts, _total = get_active_alerts(db, scope="project", scope_id=pid)
            assert alerts == []

    def test_viewer_cannot_cancel(self, viewer_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid, status="pending")
        csrf = csrf_of(viewer_client, "/renders")

        resp = viewer_client.post(
            f"/renders/{job_id}/cancel",
            data={"csrf_token": csrf},
            headers=_FORM,
            follow_redirects=False,
        )

        assert resp.status_code == 403


class TestApiCancelWithSessionCookie:
    def test_api_cancel_of_pending_render_with_cookie_session(
        self, admin_client: TestClient
    ) -> None:
        """A browser-session caller of the API cancel must not hit a locked DB.

        A cookie-authenticated request touches its session row (taking SQLite's
        write lock) before the route runs; the worker flips the pending job in
        its own transaction, so the route must end the request's transaction
        before handing off, or the worker's write waits out the lock and fails.
        """
        pid = _seed_project()
        job_id = _seed_job(pid, status="pending")
        csrf = csrf_of(admin_client, "/renders")
        token = ensure_local_token(get_context().settings)

        # The API router wants the local token; the session cookie (sent by
        # the logged-in client) is what resolves the acting principal.
        resp = admin_client.post(
            f"/api/v1/renders/{job_id}/cancel",
            headers={
                "Authorization": f"Bearer {token}",
                "X-CSRF-Token": csrf,
                "Accept": "application/json",
            },
        )

        assert resp.status_code == 200, resp.text[:300]
        assert resp.json()["status"] == "failed"
        with session_scope(get_context().session_factory) as db:
            row = db.get(RenderJob, job_id)
            assert row is not None and row.status == "failed"


# ---------------------------------------------------------------------------
# Lists
# ---------------------------------------------------------------------------


class TestLists:
    def test_empty_renders_page_keeps_the_list_but_does_not_poll(
        self, operator_client: TestClient
    ) -> None:
        """With no renders the list element is rendered, refreshed only on return.

        Nothing is pending or encoding, so no ``every`` trigger (#67 P5): the
        list refreshes only when the tab is shown again (``tlm:visible``).
        """
        html = operator_client.get("/renders").text

        list_html = html[html.index('id="renders-list"') :]
        assert 'hx-get="/partials/renders/list?poll=1"' in html
        assert 'hx-trigger="tlm:visible from:document"' in html
        assert "every 10s" not in html
        assert 'hx-select="#renders-list"' in html
        assert "data-poll" in list_html
        # The empty state lives inside the list, so a refresh that finds
        # renders again replaces it.
        assert list_html.index("No renders yet") < list_html.index(
            'id="job-card-modal"'
        )
        assert "Go to Projects" in html
        assert 'id="renders-more"' not in html

    def test_deleting_the_last_render_leaves_a_refreshable_list(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        job_id = _seed_job(pid)
        csrf = csrf_of(admin_client, "/renders")
        admin_client.post(
            f"/renders/{job_id}/delete",
            data={"csrf_token": csrf},
            headers={**_FORM, **_HTMX},
        )

        # What a refresh fetches next: it still carries #renders-list (idle,
        # so 286 and no `every` trigger).
        resp = admin_client.get("/partials/renders/list?poll=1", headers=_HTMX)

        assert resp.status_code == 286
        assert 'id="renders-list"' in resp.text
        assert 'hx-trigger="tlm:visible from:document"' in resp.text
        assert f"render-row-{job_id}" not in resp.text
        assert "No renders yet" in resp.text

    def test_renders_page_excludes_exports_and_names_projects(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project("Named Project")
        render_id = _seed_job(pid)
        export_id = _seed_job(pid, kind="export")

        html = admin_client.get("/renders").text

        assert f'id="render-row-{render_id}"' in html
        assert f"render-row-{export_id}" not in html
        assert "Named Project" in html
        # The card dialog is mounted outside the polled list.
        assert html.index('id="job-card-modal"') > html.index('id="renders-list"')
        assert f'hx-get="/renders/{render_id}/card"' in html

    def test_running_row_has_indeterminate_progress(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        _seed_job(pid, status="encoding")

        html = admin_client.get("/renders").text

        assert "progress-bar indeterminate" in html
        assert "width:50%" not in html

    def test_project_page_splits_renders_and_exports(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        render_id = _seed_job(pid)
        older_export = _seed_job(pid, kind="export")
        newer_export = _seed_job(pid, kind="export")

        html = admin_client.get(f"/projects/{pid}").text

        renders_tab = html[
            html.index('id="tab-renders"') : html.index('id="tab-settings"')
        ]
        frames_tab = html[
            html.index('id="tab-frames"') : html.index('id="tab-milestones"')
        ]
        assert f'id="render-row-{render_id}"' in renders_tab
        assert "export-row-" not in renders_tab
        assert f'id="export-row-{newer_export}"' in frames_tab
        # Newest first.
        assert frames_tab.index(f"export-row-{newer_export}") < frames_tab.index(
            f"export-row-{older_export}"
        )
        assert html.count('id="job-card-modal"') == 1
        assert 'id="toast-region"' in html


class TestExportEnqueueAudit:
    def test_export_request_records_who_started_it(
        self, operator_client: TestClient
    ) -> None:
        """POST /frames/export writes ``export N triggered`` for the card."""
        from timelapse_manager.db.models import Frame

        pid = _seed_project()
        with session_scope(get_context().session_factory) as db:
            frame = Frame(
                project_id=pid,
                sequence_index=0,
                capture_timestamp=datetime(2026, 1, 1),
                file_path="00000000.jpg",
                capture_status="captured",
                lifecycle_state="active",
            )
            db.add(frame)
            db.flush()
            frame_id = frame.id
        csrf = csrf_of(operator_client, f"/projects/{pid}")

        resp = operator_client.post(
            "/frames/export",
            data={"csrf_token": csrf, "frame_ids": str(frame_id)},
            headers=_FORM,
        )

        assert resp.status_code == 202
        job_id = resp.json()["job_id"]
        html = _card(operator_client, f"/frames/export/{job_id}/card")
        assert "Started by" in html and "operator" in html


# ---------------------------------------------------------------------------
# Renders list: pagination and polling (#67 P5)
# ---------------------------------------------------------------------------

_ACTIVE_TRIGGER = 'hx-trigger="every 10s, tlm:visible from:document"'
_IDLE_TRIGGER = 'hx-trigger="tlm:visible from:document"'


def _seed_jobs(project_id: int, count: int, **fields: object) -> list[int]:
    """Insert ``count`` renders without output files; return ids, oldest first."""
    return [_seed_job(project_id, with_file=False, **fields) for _ in range(count)]


def _row_ids(html: str) -> list[int]:
    """The render ids of the rows in ``html``, in document order."""
    return [int(m) for m in re.findall(r'id="render-row-(\d+)"', html)]


class TestRendersPagination:
    def test_page_lists_the_newest_fifty_and_offers_load_more(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        ids = _seed_jobs(pid, 52)

        html = viewer_client.get("/renders").text

        assert _row_ids(html) == ids[::-1][:50]
        oldest_shown = ids[2]
        assert f'hx-get="/partials/renders/list?before={oldest_shown}"' in html
        assert f'href="/renders?before={oldest_shown}"' in html
        # "Load more" sits outside the polled list.
        polled = html[
            html.index('id="renders-list"') : html.index('id="renders-older"')
        ]
        assert "Load more" not in polled
        assert f'hx-get="/partials/renders/list?poll=1&amp;since={oldest_shown}"' in (
            html
        )

    def test_load_more_is_keyset_on_before_and_honours_limit(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        ids = _seed_jobs(pid, 8)

        resp = viewer_client.get(
            f"/partials/renders/list?before={ids[6]}&limit=3", headers=_HTMX
        )

        assert resp.status_code == 200
        # Strictly older than ``before``, newest first, three of them.
        assert _row_ids(resp.text) == [ids[5], ids[4], ids[3]]
        # The fresh link carries on below the oldest row it returned.
        assert f'hx-get="/partials/renders/list?before={ids[3]}"' in resp.text
        assert 'id="renders-list"' not in resp.text

    def test_load_more_at_the_start_of_the_queue_has_no_link(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        ids = _seed_jobs(pid, 3)

        resp = viewer_client.get(
            f"/partials/renders/list?before={ids[2]}", headers=_HTMX
        )

        assert _row_ids(resp.text) == [ids[1], ids[0]]
        assert "Load more" not in resp.text

    @pytest.mark.parametrize("limit", [0, -5, 10_000])
    def test_limit_is_clamped(self, viewer_client: TestClient, limit: int) -> None:
        pid = _seed_project()
        ids = _seed_jobs(pid, 3)

        resp = viewer_client.get(
            f"/partials/renders/list?before={ids[-1] + 1}&limit={limit}",
            headers=_HTMX,
        )

        assert resp.status_code == 200
        assert len(_row_ids(resp.text)) == (1 if limit <= 0 else 3)

    @pytest.mark.parametrize(
        "query",
        ["before=99999999999999999999999", "poll=1&since=99999999999999999999999"],
    )
    def test_oversized_cursor_is_not_a_server_error(
        self, viewer_client: TestClient, query: str
    ) -> None:
        pid = _seed_project()
        _seed_jobs(pid, 2)

        resp = viewer_client.get(f"/partials/renders/list?{query}", headers=_HTMX)

        assert resp.status_code in (200, 286)

    def test_load_more_leaves_out_exports(self, viewer_client: TestClient) -> None:
        pid = _seed_project()
        render_id = _seed_job(pid, with_file=False)
        export_id = _seed_job(pid, kind="export", with_file=False)

        resp = viewer_client.get(
            f"/partials/renders/list?before={export_id + 1}", headers=_HTMX
        )

        assert _row_ids(resp.text) == [render_id]

    def test_no_js_fallback_page_lists_older_renders(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        ids = _seed_jobs(pid, 4)

        html = viewer_client.get(f"/renders?before={ids[2]}").text

        assert _row_ids(html) == [ids[1], ids[0]]
        # Its poll refreshes the same window.
        assert (
            f'hx-get="/partials/renders/list?poll=1&amp;before={ids[2]}'
            f'&amp;since={ids[0]}"' in html
        )

    def test_list_query_selects_no_output_settings(
        self, viewer_client: TestClient
    ) -> None:
        """Only the row's few settings keys are read, never the whole column."""
        from sqlalchemy import event

        pid = _seed_project()
        _seed_jobs(pid, 2)
        statements: list[str] = []
        engine = get_context().db_engine

        def _capture(*args: object) -> None:
            statements.append(str(args[2]))

        event.listen(engine, "before_cursor_execute", _capture)
        try:
            assert viewer_client.get("/renders").status_code == 200
        finally:
            event.remove(engine, "before_cursor_execute", _capture)

        listing = [s for s in statements if "FROM render_job" in s and "LIMIT" in s]
        assert listing, statements
        for sql in listing:
            # The column appears only inside a key extraction, never selected.
            rest = sql.replace("JSON_EXTRACT(render_job.output_settings, ?)", "")
            assert "output_settings" not in rest, sql


class TestRendersPolling:
    def test_poll_attribute_only_while_a_render_is_active(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        _seed_job(pid)
        assert _IDLE_TRIGGER in viewer_client.get("/renders").text

        _seed_job(pid, status="pending")
        assert _ACTIVE_TRIGGER in viewer_client.get("/renders").text

    def test_encoding_render_also_keeps_the_list_polling(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        _seed_job(pid, status="encoding")

        resp = viewer_client.get("/partials/renders/list?poll=1", headers=_HTMX)

        assert resp.status_code == 200
        assert _ACTIVE_TRIGGER in resp.text

    def test_an_active_export_does_not_make_the_list_poll(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        _seed_job(pid)
        _seed_job(pid, kind="export", status="encoding", with_file=False)

        resp = viewer_client.get("/partials/renders/list?poll=1", headers=_HTMX)

        assert resp.status_code == 286
        assert _IDLE_TRIGGER in resp.text

    def test_idle_poll_answers_286_with_the_final_list(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        done = _seed_job(pid)
        failed = _seed_job(pid, status="failed", with_file=False)

        resp = viewer_client.get("/partials/renders/list?poll=1", headers=_HTMX)

        assert resp.status_code == 286
        assert _row_ids(resp.text) == [failed, done]
        assert "every 10s" not in resp.text
        assert "data-poll" in resp.text

    def test_poll_keeps_every_row_from_since_and_adds_new_ones(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        ids = _seed_jobs(pid, 5)
        new = _seed_job(pid, status="pending", with_file=False)

        resp = viewer_client.get(
            f"/partials/renders/list?poll=1&since={ids[1]}", headers=_HTMX
        )

        assert resp.status_code == 200
        assert _row_ids(resp.text) == [new, ids[4], ids[3], ids[2], ids[1]]
        assert 'hx-get="/partials/renders/list?poll=1&amp;since=' in resp.text
        assert f"since={ids[1]}" in resp.text

    def test_window_emptied_by_deletes_does_not_claim_no_renders(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        older = _seed_job(pid, with_file=False)

        resp = viewer_client.get(
            f"/partials/renders/list?poll=1&since={older + 1}", headers=_HTMX
        )

        assert resp.status_code == 286
        assert _row_ids(resp.text) == []
        assert "No renders yet" not in resp.text

    def test_load_more_never_answers_286(self, viewer_client: TestClient) -> None:
        pid = _seed_project()
        ids = _seed_jobs(pid, 2)

        resp = viewer_client.get(
            f"/partials/renders/list?before={ids[1]}", headers=_HTMX
        )

        assert resp.status_code == 200

    def test_partial_requires_a_session(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/partials/renders/list?poll=1", follow_redirects=False)

        assert resp.status_code in (302, 303, 401)
