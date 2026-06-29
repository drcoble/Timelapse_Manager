"""Web edit / clone / delete flows for an existing project.

These exercise the admin-gated, CSRF-protected management routes end to end
through the running app (a real session cookie + form token). The capture
supervisor is constructed but not started in the web test settings
(``capture.autostart=False``), so ``notify_reconcile()`` is a safe no-op here
unless a test swaps in a mock to assert the seam.
"""

from __future__ import annotations

import re
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Frame, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.storage import paths


def _seed_camera(*, name: str, protocol: str | None = "vapix") -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address="127.0.0.1",
            protocol=protocol,
            snapshot_uri="http://127.0.0.1/snap",
        )
        db.add(cam)
        db.flush()
        return cam.id


def _seed_project(*, name: str, camera_id: int, interval: int = 60) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = Project(
            camera_id=camera_id,
            name=name,
            capture_interval_seconds=interval,
            lifecycle_state="active",
        )
        db.add(proj)
        db.flush()
        return proj.id


def _project(project_id: int) -> Project | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.get(Project, project_id)


def _project_named(name: str) -> Project | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.query(Project).filter(Project.name == name).one_or_none()


class TestEditProjectForm:
    def test_get_settings_form_renders(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="edit-form-cam")
        pid = _seed_project(name="Editable", camera_id=cam, interval=77)
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200
        # The form must prefill the project's current configuration so an admin
        # edits from the existing values, not blanks.
        assert "Editable" in resp.text
        assert "77" in resp.text
        assert f'value="{cam}" selected' in resp.text

    def test_settings_form_prefills_decomposed_value_and_unit(
        self, admin_client: TestClient
    ) -> None:
        # 7200s is a clean 2 hours, so the form must prefill value=2 with hours
        # preselected -- not the raw 7200 seconds.
        cam = _seed_camera(name="prefill-cam")
        pid = _seed_project(name="Prefill", camera_id=cam, interval=7200)
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200
        assert 'name="capture_interval_value"' in resp.text
        assert 'value="2"' in resp.text
        assert '<option value="hours" selected>' in resp.text

    def test_settings_form_prefills_months_for_clean_month(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="prefill-month-cam")
        pid = _seed_project(name="PrefillMonth", camera_id=cam, interval=2592000)
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200
        assert 'value="1"' in resp.text
        assert '<option value="months" selected>' in resp.text

    def test_get_settings_not_shadowed_by_action_catchall(
        self, admin_client: TestClient
    ) -> None:
        # ``/projects/{id}/settings`` must route to the edit form, not be swallowed
        # by the ``POST /projects/{id}/{action}`` catch-all (which is POST-only,
        # but a registration-order regression could still mis-route a GET).
        cam = _seed_camera(name="edit-shadow-cam")
        pid = _seed_project(name="NotShadowed", camera_id=cam)
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200


class TestEditProjectSubmit:
    def test_valid_update_redirects_and_persists(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="edit-ok-cam")
        pid = _seed_project(name="Before Edit", camera_id=cam)
        csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data={
                "name": "After Edit",
                "camera_id": str(cam),
                "capture_interval_value": "300",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/projects/{pid}"
        project = _project(pid)
        assert project is not None
        assert project.name == "After Edit"
        assert project.capture_interval_seconds == 300

    def test_update_notifies_supervisor(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="edit-notify-cam")
        pid = _seed_project(name="Notify Edit", camera_id=cam)
        ctx = get_context()
        previous = ctx.capture_supervisor
        mock_supervisor = MagicMock()
        ctx.capture_supervisor = mock_supervisor
        try:
            csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
            resp = admin_client.post(
                f"/projects/{pid}/settings",
                data={
                    "name": "Notify Edit",
                    "camera_id": str(cam),
                    "capture_interval_value": "90",
                    "capture_interval_unit": "seconds",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            mock_supervisor.notify_reconcile.assert_called_once_with()
        finally:
            ctx.capture_supervisor = previous

    def test_duplicate_name_is_rejected_with_400_not_500(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="edit-dup-cam")
        _seed_project(name="Existing One", camera_id=cam)
        pid = _seed_project(name="Rename Me", camera_id=cam)
        csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data={
                "name": "Existing One",
                "camera_id": str(cam),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        # Unchanged on disk.
        assert _project(pid) is not None and _project(pid).name == "Rename Me"

    def test_same_name_is_allowed(self, admin_client: TestClient) -> None:
        # Re-saving the project's own name must not false-positive as a dup.
        cam = _seed_camera(name="edit-same-cam")
        pid = _seed_project(name="Same Name", camera_id=cam)
        csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data={
                "name": "Same Name",
                "camera_id": str(cam),
                "capture_interval_value": "120",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _project(pid).capture_interval_seconds == 120

    def test_bad_interval_is_rejected_with_400(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="edit-badint-cam")
        pid = _seed_project(name="Bad Interval Edit", camera_id=cam)
        csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data={
                "name": "Bad Interval Edit",
                "camera_id": str(cam),
                "capture_interval_value": "0",
                "capture_interval_unit": "seconds",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    def test_unit_edit_converts_to_seconds(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="edit-unit-cam")
        pid = _seed_project(name="Unit Edit", camera_id=cam, interval=60)
        csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data={
                "name": "Unit Edit",
                "camera_id": str(cam),
                "capture_interval_value": "2",
                "capture_interval_unit": "hours",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert _project(pid).capture_interval_seconds == 7200

    def test_bad_unit_edit_rejected_and_db_unchanged(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="edit-badunit-cam")
        pid = _seed_project(name="Bad Unit Edit", camera_id=cam, interval=60)
        csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data={
                "name": "Bad Unit Edit Changed",
                "camera_id": str(cam),
                "capture_interval_value": "5",
                "capture_interval_unit": "fortnights",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        # The rejected edit must not have mutated the row.
        project = _project(pid)
        assert project.name == "Bad Unit Edit"
        assert project.capture_interval_seconds == 60

    def test_missing_csrf_token_is_forbidden(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="edit-csrf-cam")
        pid = _seed_project(name="CSRF Edit", camera_id=cam)
        resp = admin_client.post(
            f"/projects/{pid}/settings",
            data={
                "name": "CSRF Edit Changed",
                "camera_id": str(cam),
                "capture_interval_value": "60",
                "capture_interval_unit": "seconds",
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert _project(pid).name == "CSRF Edit"


class TestCloneProject:
    def test_get_clone_form_renders(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="clone-form-cam")
        pid = _seed_project(name="Clonable", camera_id=cam)
        resp = admin_client.get(f"/projects/{pid}/clone")
        assert resp.status_code == 200

    def test_clone_creates_new_project_with_copied_config(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="clone-cam")
        pid = _seed_project(name="Clone Source", camera_id=cam, interval=240)
        csrf = csrf_of(admin_client, f"/projects/{pid}/clone")
        resp = admin_client.post(
            f"/projects/{pid}/clone",
            data={"name": "Clone Target", "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        clone = _project_named("Clone Target")
        assert clone is not None
        assert clone.id != pid
        assert clone.camera_id == cam
        assert clone.capture_interval_seconds == 240
        assert clone.lifecycle_state == "active"
        assert clone.frame_count == 0
        assert resp.headers["location"] == f"/projects/{clone.id}"

    def test_clone_missing_csrf_is_forbidden(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="clone-csrf-cam")
        pid = _seed_project(name="Clone NoCSRF", camera_id=cam)
        resp = admin_client.post(
            f"/projects/{pid}/clone",
            data={"name": "Clone NoCSRF Target"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert _project_named("Clone NoCSRF Target") is None


class TestDeleteProject:
    def test_delete_redirects_and_removes_project_and_frame_file(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="delete-cam")
        pid = _seed_project(name="Delete Target", camera_id=cam)

        ctx = get_context()
        # Write a real frame file under the default per-project directory and a
        # matching row; also a finished render with a real output file. The delete
        # must clean up both file kinds and the default directory.
        frame_dir = paths.frames_root(ctx.settings) / str(pid)
        frame_dir.mkdir(parents=True, exist_ok=True)
        frame_file = frame_dir / "000001.jpg"
        frame_file.write_bytes(b"\xff\xd8\xff\xd9")
        render_file = frame_dir / "out.mp4"
        render_file.write_bytes(b"\x00\x00\x00\x18ftypmp42")
        with session_scope(ctx.session_factory) as db:
            db.add(
                Frame(
                    project_id=pid,
                    sequence_index=1,
                    file_path="000001.jpg",
                    lifecycle_state="active",
                )
            )
            db.add(
                RenderJob(
                    project_id=pid,
                    kind="manual",
                    status="done",
                    output_file_path=str(render_file),
                )
            )
            db.flush()

        csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
        resp = admin_client.post(
            f"/projects/{pid}/delete",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert resp.headers["location"] == "/projects"

        assert _project(pid) is None
        # Child rows cascaded; both files unlinked; default directory reaped.
        with session_scope(ctx.session_factory) as db:
            frames_left = db.query(Frame).filter(Frame.project_id == pid).count()
            renders_left = (
                db.query(RenderJob).filter(RenderJob.project_id == pid).count()
            )
        assert frames_left == 0
        assert renders_left == 0
        assert not frame_file.exists()
        assert not render_file.exists()

    def test_delete_notifies_supervisor(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="delete-notify-cam")
        pid = _seed_project(name="Delete Notify", camera_id=cam)
        ctx = get_context()
        previous = ctx.capture_supervisor
        mock_supervisor = MagicMock()
        ctx.capture_supervisor = mock_supervisor
        try:
            csrf = csrf_of(admin_client, f"/projects/{pid}/settings")
            resp = admin_client.post(
                f"/projects/{pid}/delete",
                data={"csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
            assert resp.status_code == 303
            mock_supervisor.notify_reconcile.assert_called_once_with()
        finally:
            ctx.capture_supervisor = previous

    def test_delete_missing_csrf_is_forbidden(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="delete-csrf-cam")
        pid = _seed_project(name="Delete CSRF", camera_id=cam)
        resp = admin_client.post(
            f"/projects/{pid}/delete",
            data={},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        assert _project(pid) is not None


def _post_action(
    admin_client: TestClient, pid: int, action: str, *, mock_supervisor: object
) -> object:
    """POST a lifecycle action with a real CSRF token and the supervisor seam.

    Swaps a mock supervisor into the runtime context for the duration of the
    request so the notify seam can be asserted, then restores the previous one.
    """
    ctx = get_context()
    previous = ctx.capture_supervisor
    ctx.capture_supervisor = mock_supervisor
    try:
        csrf = csrf_of(admin_client, f"/projects/{pid}")
        return admin_client.post(
            f"/projects/{pid}/{action}",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
    finally:
        ctx.capture_supervisor = previous


class TestProjectLifecycleActions:
    """The pause/start/resume/stop/archive/reactivate control routes.

    Each action sets the persisted lifecycle_state, commits, and wakes the
    supervisor so its reconcile loop converges the running capture tasks. ``stop``
    collapses to the same ``paused`` state as ``pause`` (with an always-on
    supervisor there is no separate stopped-but-resumable runtime state).
    """

    def test_pause_sets_paused_and_notifies(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="act-pause-cam")
        pid = _seed_project(name="Act Pause", camera_id=cam)
        mock_supervisor = MagicMock()
        resp = _post_action(admin_client, pid, "pause", mock_supervisor=mock_supervisor)
        assert resp.status_code == 303
        assert resp.headers["location"] == f"/projects/{pid}"
        assert _project(pid).lifecycle_state == "paused"
        mock_supervisor.notify_reconcile.assert_called_once_with()

    def test_stop_collapses_to_paused_and_notifies(
        self, admin_client: TestClient
    ) -> None:
        # stop == pause at runtime: both land the project in the paused state.
        cam = _seed_camera(name="act-stop-cam")
        pid = _seed_project(name="Act Stop", camera_id=cam)
        mock_supervisor = MagicMock()
        resp = _post_action(admin_client, pid, "stop", mock_supervisor=mock_supervisor)
        assert resp.status_code == 303
        assert _project(pid).lifecycle_state == "paused"
        mock_supervisor.notify_reconcile.assert_called_once_with()

    def test_resume_sets_active_and_notifies(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="act-resume-cam")
        pid = _seed_project(name="Act Resume", camera_id=cam)
        # Start paused so resume has something to undo.
        with session_scope(get_context().session_factory) as db:
            db.get(Project, pid).lifecycle_state = "paused"
        mock_supervisor = MagicMock()
        resp = _post_action(
            admin_client, pid, "resume", mock_supervisor=mock_supervisor
        )
        assert resp.status_code == 303
        assert _project(pid).lifecycle_state == "active"
        mock_supervisor.notify_reconcile.assert_called_once_with()

    def test_start_sets_active_and_notifies(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="act-start-cam")
        pid = _seed_project(name="Act Start", camera_id=cam)
        with session_scope(get_context().session_factory) as db:
            db.get(Project, pid).lifecycle_state = "paused"
        mock_supervisor = MagicMock()
        resp = _post_action(admin_client, pid, "start", mock_supervisor=mock_supervisor)
        assert resp.status_code == 303
        assert _project(pid).lifecycle_state == "active"
        mock_supervisor.notify_reconcile.assert_called_once_with()

    def test_archive_sets_archived_and_notifies(self, admin_client: TestClient) -> None:
        # Regression lock for the prior no-notify latency bug: archive must wake
        # the supervisor so capture stops promptly, not just on the next scan.
        cam = _seed_camera(name="act-archive-cam")
        pid = _seed_project(name="Act Archive", camera_id=cam)
        mock_supervisor = MagicMock()
        resp = _post_action(
            admin_client, pid, "archive", mock_supervisor=mock_supervisor
        )
        assert resp.status_code == 303
        assert _project(pid).lifecycle_state == "archived"
        mock_supervisor.notify_reconcile.assert_called_once_with()

    def test_reactivate_sets_active_and_notifies(
        self, admin_client: TestClient
    ) -> None:
        # Regression lock: reactivate must also wake the supervisor.
        cam = _seed_camera(name="act-reactivate-cam")
        pid = _seed_project(name="Act Reactivate", camera_id=cam)
        with session_scope(get_context().session_factory) as db:
            db.get(Project, pid).lifecycle_state = "archived"
        mock_supervisor = MagicMock()
        resp = _post_action(
            admin_client, pid, "reactivate", mock_supervisor=mock_supervisor
        )
        assert resp.status_code == 303
        assert _project(pid).lifecycle_state == "active"
        mock_supervisor.notify_reconcile.assert_called_once_with()

    def test_unknown_action_is_404(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="act-unknown-cam")
        pid = _seed_project(name="Act Unknown", camera_id=cam)
        csrf = csrf_of(admin_client, f"/projects/{pid}")
        resp = admin_client.post(
            f"/projects/{pid}/frobnicate",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 404
        assert _project(pid).lifecycle_state == "active"


def _set_render_schedule(project_id: int, schedule: dict | None) -> None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        db.get(Project, project_id).render_schedule = schedule


def _post_edit(client: TestClient, pid: int, cam: int, **extra: str):  # type: ignore[no-untyped-def]
    """POST the edit form with the required fields plus any extra fields."""
    csrf = csrf_of(client, f"/projects/{pid}/settings")
    data = {
        "name": f"P{pid}",
        "camera_id": str(cam),
        "capture_interval_value": "60",
        "capture_interval_unit": "seconds",
        "csrf_token": csrf,
        **extra,
    }
    return client.post(
        f"/projects/{pid}/settings",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=False,
    )


class TestEditProjectRenderSettings:
    """The structured render-settings dropdowns on the edit form."""

    def test_form_prefills_all_controls_from_stored_settings(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="render-prefill-cam")
        pid = _seed_project(name="Render Prefill", camera_id=cam)
        _set_render_schedule(
            pid,
            {
                "enabled": True,
                "interval_seconds": 21600,
                "encoder": "libx265",
                "container": "mkv",
                "fps": 30,
                "resolution": "2560x1440",
            },
        )
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200
        text = resp.text
        # Each of the six controls reflects the stored value (checkbox checked,
        # and the matching option preselected). The checkbox check targets the
        # render_enabled input specifically rather than a stray "checked" anywhere
        # on the page.
        checkbox = re.search(r'<input[^>]*id="render_enabled"[^>]*>', text)
        assert checkbox is not None
        assert "checked" in checkbox.group(0)
        assert '<option value="21600" selected>' in text
        assert '<option value="libx265" selected>' in text
        assert '<option value="mkv" selected>' in text
        # Frame rate is now a number input prefilled with the stored value.
        fps_input = re.search(r'<input[^>]*id="render_fps"[^>]*>', text)
        assert fps_input is not None
        assert 'value="30"' in fps_input.group(0)
        assert '<option value="2560x1440" selected>' in text

    def test_null_render_schedule_renders_defaults(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="render-null-cam")
        pid = _seed_project(name="Render Null", camera_id=cam)
        # No render_schedule set at all -> the form renders the defaults rather
        # than crashing.
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200
        text = resp.text
        assert '<option value="libx264" selected>' in text
        assert '<option value="mp4" selected>' in text
        fps_input = re.search(r'<input[^>]*id="render_fps"[^>]*>', text)
        assert fps_input is not None
        assert 'value="24"' in fps_input.group(0)
        assert '<option value="1920x1080" selected>' in text

    def test_old_shape_render_schedule_renders_defaults(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="render-oldshape-cam")
        pid = _seed_project(name="Render OldShape", camera_id=cam)
        # An older project whose schedule predates the dropdowns (no encoder/
        # container/etc.) must still render without crashing, falling back to the
        # encode defaults while preserving the stored enabled/interval.
        _set_render_schedule(pid, {"enabled": True, "interval_seconds": 3600})
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200
        text = resp.text
        assert '<option value="3600" selected>' in text
        assert '<option value="libx264" selected>' in text
        assert '<option value="mp4" selected>' in text

    def test_save_persists_render_settings(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="render-set-cam")
        pid = _seed_project(name="Render Set", camera_id=cam)
        resp = _post_edit(
            admin_client,
            pid,
            cam,
            render_enabled="on",
            render_frequency="3600",
            render_encoder="libx265",
            render_container="mkv",
            render_fps="30",
            render_resolution="2560x1440",
            # Auto-prune is on the form now; the marker plus a checked box persist
            # it as enabled (and the round-trip echoes both stored keys).
            render_autoprune_present="1",
            render_autoprune="on",
        )
        assert resp.status_code == 303
        assert _project(pid).render_schedule == {
            "enabled": True,
            "interval_seconds": 3600,
            "encoder": "libx265",
            "container": "mkv",
            "fps": 30,
            "resolution": "2560x1440",
            "auto_prune": True,
            "autoprune": True,
            "auto_chapters": "none",
        }

    def test_unchecked_enabled_persists_disabled(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="render-disabled-cam")
        pid = _seed_project(name="Render Disabled", camera_id=cam)
        # No render_enabled field submitted (unchecked box) -> enabled False.
        resp = _post_edit(
            admin_client,
            pid,
            cam,
            render_frequency="86400",
            render_encoder="libx264",
            render_container="mp4",
            render_fps="24",
            render_resolution="source",
        )
        assert resp.status_code == 303
        stored = _project(pid).render_schedule
        assert stored["enabled"] is False
        assert stored["resolution"] == "source"

    def test_invalid_combination_rejected_with_400_and_unchanged(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="render-badcombo-cam")
        pid = _seed_project(name="Render BadCombo", camera_id=cam)
        before = {"enabled": False, "interval_seconds": 60}
        _set_render_schedule(pid, before)
        # VP9 cannot be muxed into MP4 -> rejected server-side, project unchanged.
        resp = _post_edit(
            admin_client,
            pid,
            cam,
            render_enabled="on",
            render_frequency="86400",
            render_encoder="libvpx-vp9",
            render_container="mp4",
            render_fps="24",
            render_resolution="1920x1080",
        )
        assert resp.status_code == 400
        assert "cannot be stored" in resp.text
        assert _project(pid).render_schedule == before

    def test_encoder_select_offers_av1(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="render-av1-cam")
        pid = _seed_project(name="Render AV1", camera_id=cam)
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200
        assert '<option value="libsvtav1"' in resp.text

    def test_fps_is_number_input_with_suggestion_chips(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="render-fpschips-cam")
        # A sub-hour interval yields chips; assert the input is a number field and
        # at least one suggestion chip drives the helper.
        pid = _seed_project(name="Render FpsChips", camera_id=cam, interval=30)
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200
        text = resp.text
        fps_input = re.search(r'<input[^>]*id="render_fps"[^>]*>', text)
        assert fps_input is not None
        assert 'type="number"' in fps_input.group(0)
        assert "tlmSetRenderFps(" in text

    def test_out_of_range_fps_rejected_with_400_and_unchanged(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="render-badfps-cam")
        pid = _seed_project(name="Render BadFps", camera_id=cam)
        before = {"enabled": False, "interval_seconds": 60}
        _set_render_schedule(pid, before)
        resp = _post_edit(
            admin_client,
            pid,
            cam,
            render_enabled="on",
            render_frequency="86400",
            render_encoder="libx264",
            render_container="mp4",
            render_fps="9999",  # out of range -> rejected, not silently clamped
            render_resolution="1920x1080",
        )
        assert resp.status_code == 400
        assert "Frame rate must be between" in resp.text
        assert _project(pid).render_schedule == before

    def test_autoprune_checked_persists_true(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="render-prune-on-cam")
        pid = _seed_project(name="Render PruneOn", camera_id=cam)
        resp = _post_edit(
            admin_client,
            pid,
            cam,
            render_frequency="86400",
            render_encoder="libx264",
            render_container="mp4",
            render_fps="24",
            render_resolution="1920x1080",
            render_autoprune_present="1",
            render_autoprune="on",
        )
        assert resp.status_code == 303
        assert _project(pid).render_schedule["auto_prune"] is True

    def test_autoprune_unchecked_persists_false(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="render-prune-off-cam")
        pid = _seed_project(name="Render PruneOff", camera_id=cam)
        # Marker present, checkbox absent (unticked) -> auto_prune stored as False.
        resp = _post_edit(
            admin_client,
            pid,
            cam,
            render_frequency="86400",
            render_encoder="libx264",
            render_container="mp4",
            render_fps="24",
            render_resolution="1920x1080",
            render_autoprune_present="1",
        )
        assert resp.status_code == 303
        assert _project(pid).render_schedule["auto_prune"] is False

    def test_autoprune_checkbox_prefills_from_stored(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="render-prune-prefill-cam")
        pid = _seed_project(name="Render PrunePrefill", camera_id=cam)
        _set_render_schedule(
            pid, {"enabled": False, "interval_seconds": 60, "auto_prune": False}
        )
        resp = admin_client.get(f"/projects/{pid}/settings")
        assert resp.status_code == 200
        checkbox = re.search(r'<input[^>]*id="render_autoprune"[^>]*>', resp.text)
        assert checkbox is not None
        # Stored False -> the box is not checked.
        assert "checked" not in checkbox.group(0)


class TestRenderComboCheck:
    """The live encoder/container advisory endpoint."""

    def test_download_only_notice_for_av1(self, admin_client: TestClient) -> None:
        resp = admin_client.get(
            "/renders/combo-check",
            params={"render_encoder": "libsvtav1", "render_container": "mp4"},
        )
        assert resp.status_code == 200
        # AV1 is muxable into MP4 but not browser-streamable -> info notice, no
        # mux warning.
        assert "Download only" in resp.text
        assert "cannot be stored" not in resp.text

    def test_streamable_combo_returns_empty(self, admin_client: TestClient) -> None:
        resp = admin_client.get(
            "/renders/combo-check",
            params={"render_encoder": "libx264", "render_container": "mp4"},
        )
        assert resp.status_code == 200
        assert resp.text.strip() == ""

    def test_unmuxable_combo_shows_only_warning(self, admin_client: TestClient) -> None:
        resp = admin_client.get(
            "/renders/combo-check",
            params={"render_encoder": "libvpx-vp9", "render_container": "mp4"},
        )
        assert resp.status_code == 200
        # VP9 + MP4 cannot be muxed: show the mux warning, not the download notice.
        assert "cannot be stored" in resp.text
        assert "Download only" not in resp.text


class TestManualRenderUsesStoredSettings:
    """The manual "render now" action honours the project's render settings."""

    def _trigger(self, client: TestClient, pid: int):  # type: ignore[no-untyped-def]
        csrf = csrf_of(client, f"/projects/{pid}")
        return client.post(
            f"/projects/{pid}/renders",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

    def _latest_job(self, pid: int) -> RenderJob | None:
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            return (
                db.query(RenderJob)
                .filter(RenderJob.project_id == pid)
                .order_by(RenderJob.id.desc())
                .first()
            )

    def test_manual_render_uses_stored_render_settings(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="manual-settings-cam")
        pid = _seed_project(name="Manual Settings", camera_id=cam)
        _set_render_schedule(
            pid,
            {
                "enabled": False,
                "interval_seconds": 86400,
                "encoder": "libx265",
                "container": "mkv",
                "fps": 30,
                "resolution": "1280x720",
            },
        )
        resp = self._trigger(admin_client, pid)
        assert resp.status_code == 303
        job = self._latest_job(pid)
        assert job is not None
        assert job.output_settings == {
            "fps": 30,
            "codec": "libx265",
            "container": "mkv",
            "width": 1280,
            "height": 720,
        }

    def test_manual_render_source_resolution_omits_dimensions(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="manual-source-cam")
        pid = _seed_project(name="Manual Source", camera_id=cam)
        _set_render_schedule(
            pid,
            {
                "enabled": False,
                "interval_seconds": 86400,
                "encoder": "libx264",
                "container": "mp4",
                "fps": 24,
                "resolution": "source",
            },
        )
        resp = self._trigger(admin_client, pid)
        assert resp.status_code == 303
        job = self._latest_job(pid)
        assert job is not None
        assert "width" not in job.output_settings
        assert "height" not in job.output_settings

    def test_manual_render_falls_back_to_default_without_settings(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="manual-default-cam")
        pid = _seed_project(name="Manual Default", camera_id=cam)
        resp = self._trigger(admin_client, pid)
        assert resp.status_code == 303
        job = self._latest_job(pid)
        assert job is not None
        # The hardcoded safe default applies when no render settings are stored.
        assert job.output_settings["codec"] == "h264"
        assert job.output_settings["container"] == "mp4"
        assert job.output_settings["width"] == 1920


class TestEditProjectScheduleJson:
    def test_set_post_render_actions_persists(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="pa-set-cam")
        pid = _seed_project(name="PA Set", camera_id=cam)
        resp = _post_edit(
            admin_client,
            pid,
            cam,
            post_render_actions='[{"type": "prune", "keep": 3}]',
        )
        assert resp.status_code == 303
        assert _project(pid).post_render_actions == [{"type": "prune", "keep": 3}]

    def test_invalid_archive_json_is_rejected(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="sched-badjson-cam")
        pid = _seed_project(name="Sched BadJSON", camera_id=cam)
        resp = _post_edit(admin_client, pid, cam, archive_schedule="{not json")
        assert resp.status_code == 400
        assert "invalid JSON" in resp.text

    def test_enabled_archive_schedule_requires_interval(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="sched-nointerval-cam")
        pid = _seed_project(name="Sched NoInterval", camera_id=cam)
        resp = _post_edit(admin_client, pid, cam, archive_schedule='{"enabled": true}')
        assert resp.status_code == 400
        assert "interval_seconds" in resp.text

    def test_post_actions_must_have_type(self, admin_client: TestClient) -> None:
        cam = _seed_camera(name="pa-notype-cam")
        pid = _seed_project(name="PA NoType", camera_id=cam)
        resp = _post_edit(admin_client, pid, cam, post_render_actions='[{"keep": 2}]')
        assert resp.status_code == 400
        assert "type" in resp.text

    def test_blank_archive_field_clears_schedule(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera(name="sched-clear-cam")
        pid = _seed_project(name="Sched Clear", camera_id=cam)
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            db.get(Project, pid).archive_schedule = {
                "enabled": True,
                "interval_seconds": 60,
            }
        resp = _post_edit(admin_client, pid, cam, archive_schedule="")
        assert resp.status_code == 303
        assert _project(pid).archive_schedule is None
