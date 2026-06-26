"""Integration tests for post-render actions: export, webhook, prune, Docker skip."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Event, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.render.post_actions import run_post_actions


@pytest.fixture(autouse=True)
def _force_not_under_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the Docker probe to ``False`` so the behavior tests are hermetic.

    The engine disables every post-render action when it detects a container,
    so the tests that assert export/webhook/prune actually run must not depend
    on where pytest happens to execute. Without this they pass on a bare host
    but fail inside a containerized CI runner (``/.dockerenv`` is present). The
    two tests that exercise the Docker gate itself re-patch the probe in their
    own bodies, which overrides this default.
    """
    import timelapse_manager.render.post_actions as _mod

    monkeypatch.setattr(_mod, "running_under_docker", lambda: False)


# ---------------------------------------------------------------------------
# Helpers: seed DB rows
# ---------------------------------------------------------------------------


def _seed_project_and_job(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    *,
    output_file: Path | None = None,
) -> tuple[int, int]:
    """Insert Camera, Project, and a done RenderJob; return (project_id, job_id)."""
    frames_root = settings.paths.frames_root
    assert frames_root is not None
    with session_scope(factory) as session:
        cam = Camera(name="pa-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()

        proj = Project(
            camera_id=cam.id,
            name="pa-project",
            lifecycle_state="active",
            operational_status="idle",
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

        # Ensure frame directory exists.
        frame_dir = frames_root / str(project_id)
        frame_dir.mkdir(parents=True, exist_ok=True)

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
            completed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        if output_file is not None:
            job.output_file_path = str(output_file)
        session.add(job)
        session.flush()
        job_id = job.id

    return project_id, job_id


def _make_output_file(render_root: Path, name: str = "render-1.mp4") -> Path:
    render_root.mkdir(parents=True, exist_ok=True)
    path = render_root / name
    path.write_bytes(b"\x00" * 1024)
    return path


def _count_events(factory: sessionmaker, project_id: int) -> int:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        return (
            session.query(Event)
            .filter(Event.scope == "project", Event.scope_id == project_id)
            .count()
        )


# ---------------------------------------------------------------------------
# Webhook loopback server
# ---------------------------------------------------------------------------


class _WebhookCapture(BaseHTTPRequestHandler):
    """HTTP handler that captures the first POST body."""

    received: list[bytes] = []

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        self.__class__.received.append(body)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args: object) -> None:  # pragma: no cover
        pass  # suppress server log noise in test output


def _start_webhook_server() -> tuple[HTTPServer, int]:
    """Start a loopback HTTP server and return (server, port)."""
    _WebhookCapture.received = []
    server = HTTPServer(("127.0.0.1", 0), _WebhookCapture)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


# ---------------------------------------------------------------------------
# Test: export action copies file to destination
# ---------------------------------------------------------------------------


async def test_export_action_copies_output_to_destination(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
) -> None:
    from timelapse_manager.render.spec import project_render_root

    _, job_id = _seed_project_and_job(migrated_factory, settings_no_autostart)

    # Locate the render root for project 1 (seeded as id=1 typically).
    with session_scope(migrated_factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        proj = session.get(Project, job.project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)

    render_root.mkdir(parents=True, exist_ok=True)
    output_file = render_root / "render-1.mp4"
    output_file.write_bytes(b"\x00" * 512)

    destination = tmp_path / "exports"
    action_specs = [{"type": "export", "destination": str(destination)}]

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_id,
        output_path=output_file,
        action_specs=action_specs,
    )

    assert (destination / "render-1.mp4").is_file()


async def test_export_to_bad_path_records_event_but_job_stays_done(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
) -> None:
    """Export to a path that is a file (not a dir) fails gracefully.

    Event is written; job status stays done.
    """
    project_id, job_id = _seed_project_and_job(migrated_factory, settings_no_autostart)

    from timelapse_manager.render.spec import project_render_root

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)

    render_root.mkdir(parents=True, exist_ok=True)
    output_file = render_root / "render-1.mp4"
    output_file.write_bytes(b"\x00" * 512)

    # Use a pre-existing FILE as the destination — mkdir(exist_ok=True) will raise.
    collision = tmp_path / "i_am_a_file"
    collision.write_text("not a dir")

    action_specs = [{"type": "export", "destination": str(collision)}]

    events_before = _count_events(migrated_factory, project_id)

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_id,
        output_path=output_file,
        action_specs=action_specs,
    )

    # An Event should have been written for the failure.
    events_after = _count_events(migrated_factory, project_id)
    assert events_after > events_before, "Expected a failure Event to be written"

    # Job must still be done.
    with session_scope(migrated_factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        assert job.status == "done"


async def test_failure_event_is_typed_for_notification_routing(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
) -> None:
    """A post-action failure records a ``postaction.failed``-typed event.

    Without the dotted type the dispatcher's routing drops the event, so the
    operator would never be notified. This pins the type so failure notifications
    can actually be delivered.
    """
    from timelapse_manager.render.spec import project_render_root

    project_id, job_id = _seed_project_and_job(migrated_factory, settings_no_autostart)
    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)
    render_root.mkdir(parents=True, exist_ok=True)
    output_file = render_root / "render-1.mp4"
    output_file.write_bytes(b"\x00" * 512)

    collision = tmp_path / "i_am_a_file"
    collision.write_text("not a dir")
    action_specs = [{"type": "export", "destination": str(collision)}]

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_id,
        output_path=output_file,
        action_specs=action_specs,
    )

    with session_scope(migrated_factory) as session:
        event = (
            session.query(Event)
            .filter(Event.scope == "project", Event.scope_id == project_id)
            .order_by(Event.id.desc())
            .first()
        )
        assert event is not None
        assert event.level == "warning"
        assert event.event_metadata is not None
        assert event.event_metadata.get("type") == "postaction.failed"


# ---------------------------------------------------------------------------
# Test: webhook action posts correct payload
# ---------------------------------------------------------------------------


async def test_webhook_posts_correct_payload(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
) -> None:
    """The webhook action POSTs the expected JSON payload to the configured URL.

    The test server binds to 127.0.0.1 (loopback). The webhook surface never
    opts in to loopback, so validate_outbound_url is mocked to a pass-through
    here. The guard's correct denial of loopback webhook targets is covered
    separately in the abuse test suite.
    """
    import json
    from unittest.mock import patch

    project_id, job_id = _seed_project_and_job(migrated_factory, settings_no_autostart)

    from timelapse_manager.render.spec import project_render_root

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)

    render_root.mkdir(parents=True, exist_ok=True)
    output_file = render_root / "render-1.mp4"
    output_file.write_bytes(b"\x00" * 512)

    server, port = _start_webhook_server()
    try:
        webhook_url = f"http://127.0.0.1:{port}/webhook"
        action_specs = [{"type": "external_trigger", "url": webhook_url}]

        with patch(
            "timelapse_manager.render.post_actions.validate_outbound_url",
            side_effect=lambda url: url,
        ):
            await run_post_actions(
                settings_no_autostart,
                migrated_factory,
                job_id=job_id,
                output_path=output_file,
                action_specs=action_specs,
            )

        assert len(_WebhookCapture.received) == 1
        body = json.loads(_WebhookCapture.received[0])
        assert body["event"] == "render_completed"
        assert body["project_id"] == project_id
        assert body["render_id"] == job_id
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# Test: prune deletes non-archive done renders beyond keep=N
# ---------------------------------------------------------------------------


async def test_prune_deletes_old_non_archive_renders_beyond_keep_count(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
) -> None:
    from timelapse_manager.render.spec import project_render_root

    project_id, _ = _seed_project_and_job(migrated_factory, settings_no_autostart)

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)

    render_root.mkdir(parents=True, exist_ok=True)

    # Create 4 done manual renders; prune with keep=2 should remove the 2 oldest.
    job_ids = []
    output_files = []
    with session_scope(migrated_factory) as session:
        for i in range(4):
            outfile = render_root / f"render-{i + 10}.mp4"
            outfile.write_bytes(b"\x00" * 128)
            output_files.append(outfile)
            j = RenderJob(
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
                output_file_path=str(outfile),
                completed_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(j)
            session.flush()
            job_ids.append(j.id)

    # Use the last job as the trigger for prune. The configured prune action is
    # trigger-exempt for manual renders, so trigger it as a scheduled render.
    action_specs = [{"type": "prune", "keep": 2}]

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_ids[-1],
        output_path=output_files[-1],
        action_specs=action_specs,
        kind="scheduled",
    )

    # Prune removes candidates[keep_count:] — ordered by id desc, oldest first.
    surviving_ids = []
    with session_scope(migrated_factory) as session:
        for jid in job_ids:
            j = session.get(RenderJob, jid)
            if j is not None:
                surviving_ids.append(jid)

    # Only 2 should survive.
    assert len(surviving_ids) == 2
    # The 2 newest (highest ids) should survive.
    assert set(surviving_ids) == {job_ids[2], job_ids[3]}


async def test_manual_trigger_skips_configured_prune_action(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
) -> None:
    """A manual render completion never runs the configured prune action.

    The prune post-action is trigger-exempt for manual renders: a one-off manual
    render must not sweep away earlier renders. Every row and output survives.
    """
    from timelapse_manager.render.spec import project_render_root

    project_id, _ = _seed_project_and_job(migrated_factory, settings_no_autostart)

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)

    render_root.mkdir(parents=True, exist_ok=True)

    job_ids = []
    output_files = []
    with session_scope(migrated_factory) as session:
        for i in range(4):
            outfile = render_root / f"render-{i + 30}.mp4"
            outfile.write_bytes(b"\x00" * 128)
            output_files.append(outfile)
            j = RenderJob(
                project_id=project_id,
                kind="manual",
                status="done",
                output_settings={},
                output_file_path=str(outfile),
                completed_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(j)
            session.flush()
            job_ids.append(j.id)

    # A configured prune with keep=1 would normally delete the three oldest --
    # but a manual trigger is exempt, so nothing is pruned.
    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_ids[-1],
        output_path=output_files[-1],
        action_specs=[{"type": "prune", "keep": 1}],
        kind="manual",
    )

    with session_scope(migrated_factory) as session:
        surviving = [jid for jid in job_ids if session.get(RenderJob, jid) is not None]
    assert surviving == job_ids, "Manual trigger must not run the configured prune"
    for f in output_files:
        assert f.is_file(), "Manual trigger must not delete any render output"


async def test_prune_never_deletes_archive_renders(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
) -> None:
    from timelapse_manager.render.spec import project_render_root

    project_id, _ = _seed_project_and_job(migrated_factory, settings_no_autostart)

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)

    render_root.mkdir(parents=True, exist_ok=True)

    # Create 3 archive renders + 3 manual renders.
    archive_ids = []
    manual_ids = []
    archive_files = []
    manual_files = []

    with session_scope(migrated_factory) as session:
        for i in range(3):
            outfile = render_root / f"archive-{i}.mp4"
            outfile.write_bytes(b"\x00" * 64)
            archive_files.append(outfile)
            j = RenderJob(
                project_id=project_id,
                kind="archive",
                status="done",
                output_settings={},
                output_file_path=str(outfile),
                completed_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(j)
            session.flush()
            archive_ids.append(j.id)

        for i in range(3):
            outfile = render_root / f"manual-{i}.mp4"
            outfile.write_bytes(b"\x00" * 64)
            manual_files.append(outfile)
            j = RenderJob(
                project_id=project_id,
                kind="manual",
                status="done",
                output_settings={},
                output_file_path=str(outfile),
                completed_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(j)
            session.flush()
            manual_ids.append(j.id)

    # Prune with keep=1 should keep newest manual only; all archives must survive.
    # The configured prune action skips manual triggers, so fire as scheduled.
    action_specs = [{"type": "prune", "keep": 1}]
    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=manual_ids[-1],
        output_path=manual_files[-1],
        action_specs=action_specs,
        kind="scheduled",
    )

    with session_scope(migrated_factory) as session:
        for aid in archive_ids:
            j = session.get(RenderJob, aid)
            assert j is not None, f"Archive render {aid} was incorrectly pruned"

    # Archive files must still exist on disk.
    for f in archive_files:
        assert f.is_file(), f"Archive file {f} was incorrectly deleted"


# ---------------------------------------------------------------------------
# Test: Docker gating is per-action -- only export is skipped in a container;
# webhook and prune run normally (they have no host-path dependency).
# ---------------------------------------------------------------------------


async def test_export_skipped_under_docker(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Under Docker the export action no-ops (no host export path), without error."""
    import timelapse_manager.render.post_actions as _mod

    monkeypatch.setattr(_mod, "running_under_docker", lambda: True)

    project_id, job_id = _seed_project_and_job(migrated_factory, settings_no_autostart)

    from timelapse_manager.render.spec import project_render_root

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)

    render_root.mkdir(parents=True, exist_ok=True)
    output_file = render_root / "render-1.mp4"
    output_file.write_bytes(b"\x00" * 64)

    destination = tmp_path / "should_not_exist"
    action_specs = [{"type": "export", "destination": str(destination)}]

    events_before = _count_events(migrated_factory, project_id)

    # Should complete without error and without writing the export.
    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_id,
        output_path=output_file,
        action_specs=action_specs,
    )

    assert not destination.exists(), "Export must not occur under Docker"
    events_after = _count_events(migrated_factory, project_id)
    assert events_after == events_before, (
        "Skipping export under Docker is not a failure and writes no event"
    )


async def test_webhook_runs_under_docker(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The webhook action still fires under Docker -- it has no host-path dependency."""
    import timelapse_manager.render.post_actions as _mod

    monkeypatch.setattr(_mod, "running_under_docker", lambda: True)
    monkeypatch.setattr(_mod, "validate_outbound_url", lambda url: url)

    project_id, job_id = _seed_project_and_job(migrated_factory, settings_no_autostart)

    from timelapse_manager.render.spec import project_render_root

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)

    render_root.mkdir(parents=True, exist_ok=True)
    output_file = render_root / "render-1.mp4"
    output_file.write_bytes(b"\x00" * 512)

    server, port = _start_webhook_server()
    try:
        action_specs = [
            {"type": "external_trigger", "url": f"http://127.0.0.1:{port}/webhook"}
        ]
        await run_post_actions(
            settings_no_autostart,
            migrated_factory,
            job_id=job_id,
            output_path=output_file,
            action_specs=action_specs,
        )
        assert len(_WebhookCapture.received) == 1, "Webhook must fire under Docker"
    finally:
        server.shutdown()


async def test_prune_runs_under_docker(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prune still runs under Docker -- it only touches the project render root."""
    import timelapse_manager.render.post_actions as _mod

    monkeypatch.setattr(_mod, "running_under_docker", lambda: True)

    project_id, _ = _seed_project_and_job(migrated_factory, settings_no_autostart)

    from timelapse_manager.render.spec import project_render_root

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)

    render_root.mkdir(parents=True, exist_ok=True)

    # Three done manual renders; prune keep=1 should remove the two oldest.
    job_ids = []
    output_files = []
    with session_scope(migrated_factory) as session:
        for i in range(3):
            outfile = render_root / f"render-{i + 20}.mp4"
            outfile.write_bytes(b"\x00" * 128)
            output_files.append(outfile)
            j = RenderJob(
                project_id=project_id,
                kind="manual",
                status="done",
                output_settings={},
                output_file_path=str(outfile),
                completed_at=datetime.now(UTC).replace(tzinfo=None),
            )
            session.add(j)
            session.flush()
            job_ids.append(j.id)

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_ids[-1],
        output_path=output_files[-1],
        action_specs=[{"type": "prune", "keep": 1}],
        kind="scheduled",
    )

    with session_scope(migrated_factory) as session:
        surviving = [jid for jid in job_ids if session.get(RenderJob, jid) is not None]
    assert surviving == [job_ids[-1]], "Prune must run under Docker, keeping newest"
    assert not output_files[0].is_file(), "Oldest render file should be pruned"


async def test_post_actions_not_skipped_when_not_under_docker(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import timelapse_manager.render.post_actions as _mod

    monkeypatch.setattr(_mod, "running_under_docker", lambda: False)

    project_id, job_id = _seed_project_and_job(migrated_factory, settings_no_autostart)

    from timelapse_manager.render.spec import project_render_root

    with session_scope(migrated_factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        render_root = project_render_root(settings_no_autostart, proj)

    render_root.mkdir(parents=True, exist_ok=True)
    output_file = render_root / "render-1.mp4"
    output_file.write_bytes(b"\x00" * 64)

    destination = tmp_path / "exports"
    action_specs = [{"type": "export", "destination": str(destination)}]

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_id,
        output_path=output_file,
        action_specs=action_specs,
    )

    assert (destination / "render-1.mp4").is_file()


# ---------------------------------------------------------------------------
# Test: empty action_specs is a no-op
# ---------------------------------------------------------------------------


async def test_no_action_specs_is_no_op(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
) -> None:
    _, job_id = _seed_project_and_job(migrated_factory, settings_no_autostart)

    # Should complete without error.
    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_id,
        output_path=tmp_path / "fake.mp4",
        action_specs=[],
    )


# ---------------------------------------------------------------------------
# Test: output_path=None is a no-op (even with action specs)
# ---------------------------------------------------------------------------


async def test_none_output_path_skips_all_actions(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
    tmp_path: Path,
) -> None:
    project_id, job_id = _seed_project_and_job(migrated_factory, settings_no_autostart)
    destination = tmp_path / "should_not_exist"
    action_specs = [{"type": "export", "destination": str(destination)}]

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=job_id,
        output_path=None,
        action_specs=action_specs,
    )

    assert not destination.exists()


# ---------------------------------------------------------------------------
# Schedule-scoped auto-prune: after a scheduled/archive render, keep only the
# latest render of that same kind for the project. Manual renders never trigger
# it and are never deleted by it. These drive the real auto_prune_enabled
# accessor through the project's stored schedules (no key = enabled).
# ---------------------------------------------------------------------------


def _seed_project_with_schedules(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    *,
    render_schedule: dict | None = None,
    archive_schedule: dict | None = None,
) -> int:
    """Insert a Camera and Project with the given schedules; return project_id."""
    frames_root = settings.paths.frames_root
    assert frames_root is not None
    with session_scope(factory) as session:
        cam = Camera(name="ap-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()

        proj = Project(
            camera_id=cam.id,
            name="ap-project",
            lifecycle_state="active",
            operational_status="idle",
            render_schedule=render_schedule,
            archive_schedule=archive_schedule,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

        frame_dir = frames_root / str(project_id)
        frame_dir.mkdir(parents=True, exist_ok=True)
    return project_id


def _add_done_render(
    factory: sessionmaker,  # type: ignore[type-arg]
    project_id: int,
    *,
    kind: str,
    output_file: Path,
) -> int:
    """Insert one done render of ``kind`` with an on-disk output; return its id."""
    output_file.write_bytes(b"\x00" * 64)
    with session_scope(factory) as session:
        job = RenderJob(
            project_id=project_id,
            kind=kind,
            status="done",
            output_settings={},
            output_file_path=str(output_file),
            completed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(job)
        session.flush()
        return job.id


def _render_root_for(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    project_id: int,
) -> Path:
    from timelapse_manager.render.spec import project_render_root

    with session_scope(factory) as session:
        proj = session.get(Project, project_id)
        assert proj is not None
        root = project_render_root(settings, proj)
    root.mkdir(parents=True, exist_ok=True)
    return root


async def test_auto_prune_scheduled_keeps_only_latest_scheduled(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A scheduled completion deletes older scheduled renders, keeping the newest."""
    project_id = _seed_project_with_schedules(
        migrated_factory, settings_no_autostart, render_schedule={"enabled": True}
    )
    root = _render_root_for(migrated_factory, settings_no_autostart, project_id)

    files = [root / f"sched-{i}.mp4" for i in range(3)]
    ids = [
        _add_done_render(migrated_factory, project_id, kind="scheduled", output_file=f)
        for f in files
    ]

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=ids[-1],
        output_path=files[-1],
        action_specs=[],
        kind="scheduled",
    )

    with session_scope(migrated_factory) as session:
        surviving = [jid for jid in ids if session.get(RenderJob, jid) is not None]
    assert surviving == [ids[-1]], "Only the latest scheduled render should remain"
    assert files[-1].is_file()
    assert not files[0].is_file(), "Older scheduled output should be deleted"
    assert not files[1].is_file(), "Older scheduled output should be deleted"


async def test_auto_prune_archive_keeps_only_latest_archive(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """An archive completion deletes older archive renders, keeping the newest."""
    project_id = _seed_project_with_schedules(
        migrated_factory, settings_no_autostart, archive_schedule={"enabled": True}
    )
    root = _render_root_for(migrated_factory, settings_no_autostart, project_id)

    files = [root / f"arch-{i}.mp4" for i in range(3)]
    ids = [
        _add_done_render(migrated_factory, project_id, kind="archive", output_file=f)
        for f in files
    ]

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=ids[-1],
        output_path=files[-1],
        action_specs=[],
        kind="archive",
    )

    with session_scope(migrated_factory) as session:
        surviving = [jid for jid in ids if session.get(RenderJob, jid) is not None]
    assert surviving == [ids[-1]], "Only the latest archive render should remain"
    assert not files[0].is_file()
    assert not files[1].is_file()


async def test_auto_prune_is_scoped_to_same_kind(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A scheduled prune never touches archive or manual rows, and vice versa."""
    project_id = _seed_project_with_schedules(
        migrated_factory,
        settings_no_autostart,
        render_schedule={"enabled": True},
        archive_schedule={"enabled": True},
    )
    root = _render_root_for(migrated_factory, settings_no_autostart, project_id)

    sched_files = [root / f"s-{i}.mp4" for i in range(2)]
    sched_ids = [
        _add_done_render(migrated_factory, project_id, kind="scheduled", output_file=f)
        for f in sched_files
    ]
    arch_files = [root / f"a-{i}.mp4" for i in range(2)]
    arch_ids = [
        _add_done_render(migrated_factory, project_id, kind="archive", output_file=f)
        for f in arch_files
    ]
    man_files = [root / f"m-{i}.mp4" for i in range(2)]
    man_ids = [
        _add_done_render(migrated_factory, project_id, kind="manual", output_file=f)
        for f in man_files
    ]

    # A scheduled completion prunes only prior scheduled renders.
    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=sched_ids[-1],
        output_path=sched_files[-1],
        action_specs=[],
        kind="scheduled",
    )

    with session_scope(migrated_factory) as session:
        sched_alive = [j for j in sched_ids if session.get(RenderJob, j) is not None]
        arch_alive = [j for j in arch_ids if session.get(RenderJob, j) is not None]
        man_alive = [j for j in man_ids if session.get(RenderJob, j) is not None]

    assert sched_alive == [sched_ids[-1]], "Only latest scheduled survives"
    assert arch_alive == arch_ids, "Archive renders untouched by a scheduled prune"
    assert man_alive == man_ids, "Manual renders untouched by a scheduled prune"
    # Manual and archive outputs all remain on disk.
    for f in arch_files + man_files:
        assert f.is_file()

    # Symmetrically, an archive completion prunes only prior archive renders.
    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=arch_ids[-1],
        output_path=arch_files[-1],
        action_specs=[],
        kind="archive",
    )

    with session_scope(migrated_factory) as session:
        arch_alive = [j for j in arch_ids if session.get(RenderJob, j) is not None]
        man_alive = [j for j in man_ids if session.get(RenderJob, j) is not None]
    assert arch_alive == [arch_ids[-1]], "Only latest archive survives"
    assert man_alive == man_ids, "Manual renders untouched by an archive prune"


async def test_manual_render_never_triggers_auto_prune(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A manual completion is trigger-exempt: no kind is ever auto-pruned by it."""
    project_id = _seed_project_with_schedules(
        migrated_factory,
        settings_no_autostart,
        render_schedule={"enabled": True},
        archive_schedule={"enabled": True},
    )
    root = _render_root_for(migrated_factory, settings_no_autostart, project_id)

    sched_files = [root / f"s-{i}.mp4" for i in range(2)]
    sched_ids = [
        _add_done_render(migrated_factory, project_id, kind="scheduled", output_file=f)
        for f in sched_files
    ]
    arch_files = [root / f"a-{i}.mp4" for i in range(2)]
    arch_ids = [
        _add_done_render(migrated_factory, project_id, kind="archive", output_file=f)
        for f in arch_files
    ]
    man_files = [root / f"m-{i}.mp4" for i in range(2)]
    man_ids = [
        _add_done_render(migrated_factory, project_id, kind="manual", output_file=f)
        for f in man_files
    ]

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=man_ids[-1],
        output_path=man_files[-1],
        action_specs=[],
        kind="manual",
    )

    with session_scope(migrated_factory) as session:
        sched_alive = [j for j in sched_ids if session.get(RenderJob, j) is not None]
        arch_alive = [j for j in arch_ids if session.get(RenderJob, j) is not None]
        man_alive = [j for j in man_ids if session.get(RenderJob, j) is not None]
    assert sched_alive == sched_ids, "Manual trigger must not prune scheduled renders"
    assert arch_alive == arch_ids, "Manual trigger must not prune archive renders"
    assert man_alive == man_ids, "Manual renders are never auto-pruned"
    for f in sched_files + arch_files + man_files:
        assert f.is_file()


async def test_auto_prune_disabled_when_schedule_opts_out(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """An explicit ``auto_prune: false`` in the schedule disables auto-prune.

    Assumes the disabling key is ``auto_prune`` (the natural match for
    ``auto_prune_enabled``). The accessor owns the exact key; the full-suite run
    against the real accessor is authoritative if the parallel author chose
    another name.
    """
    project_id = _seed_project_with_schedules(
        migrated_factory,
        settings_no_autostart,
        render_schedule={"enabled": True, "auto_prune": False},
    )
    root = _render_root_for(migrated_factory, settings_no_autostart, project_id)

    files = [root / f"sched-{i}.mp4" for i in range(3)]
    ids = [
        _add_done_render(migrated_factory, project_id, kind="scheduled", output_file=f)
        for f in files
    ]

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=ids[-1],
        output_path=files[-1],
        action_specs=[],
        kind="scheduled",
    )

    with session_scope(migrated_factory) as session:
        surviving = [jid for jid in ids if session.get(RenderJob, jid) is not None]
    assert surviving == ids, "Disabled auto-prune must keep every scheduled render"
    for f in files:
        assert f.is_file()


async def test_auto_prune_enabled_when_key_missing(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """A schedule with no auto-prune key (even ``None``) enables auto-prune."""
    # render_schedule left as None entirely: enabled-by-default.
    project_id = _seed_project_with_schedules(migrated_factory, settings_no_autostart)
    root = _render_root_for(migrated_factory, settings_no_autostart, project_id)

    files = [root / f"sched-{i}.mp4" for i in range(2)]
    ids = [
        _add_done_render(migrated_factory, project_id, kind="scheduled", output_file=f)
        for f in files
    ]

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=ids[-1],
        output_path=files[-1],
        action_specs=[],
        kind="scheduled",
    )

    with session_scope(migrated_factory) as session:
        surviving = [jid for jid in ids if session.get(RenderJob, jid) is not None]
    assert surviving == [ids[-1]], "Missing auto-prune key defaults to enabled"
