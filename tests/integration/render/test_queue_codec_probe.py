"""Render-queue tests for the codec probe and its startup backfill (#25, D14).

The prober is injected, so no ffprobe runs: these tests pin what the queue does
with a probe result (store it with the ``done`` row), with a failed probe
(``NULL`` + carry on), and which renders the one-shot backfill picks.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from tests.integration.render.test_queue import _enqueue_job, _seed_project
from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.encoder import (
    Encoder,
    OutputSettings,
    RenderResult,
    RenderSpec,
)
from timelapse_manager.encode.media_probe import MediaInfo
from timelapse_manager.render.queue import RenderQueue
from timelapse_manager.render.spec import project_render_root

_AV1 = MediaInfo(codec_string="av01.0.05M.08", mime_type="video/mp4")


class FileEncoder(Encoder):
    """Writes a placeholder output file and reports success."""

    async def validate(self, output: OutputSettings, *, has_chapters: bool) -> None:
        pass

    async def render(self, spec: RenderSpec) -> RenderResult:
        spec.output_path.parent.mkdir(parents=True, exist_ok=True)
        spec.output_path.write_bytes(b"\x00" * 64)
        return RenderResult(
            success=True,
            output_path=spec.output_path,
            duration_seconds=0.001,
            browser_streamable=False,
            codec=spec.output_settings.codec,
            container=spec.output_settings.container,
        )


class RecordingProber:
    """A prober that records what it was asked and returns a fixed answer."""

    def __init__(
        self, answer: MediaInfo | None = _AV1, *, error: Exception | None = None
    ) -> None:
        self.answer = answer
        self.error = error
        self.paths: list[Path] = []

    async def __call__(self, path: Path) -> MediaInfo | None:
        self.paths.append(path)
        if self.error is not None:
            raise self.error
        return self.answer


def _codec(factory: sessionmaker, job_id: int) -> tuple[str, str | None, str | None]:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        job = session.get(RenderJob, job_id)
        assert job is not None
        return job.status, job.codec_string, job.mime_type


async def _run_one(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    prober: RecordingProber,
) -> int:
    project_id = _seed_project(factory, settings)
    job_id = _enqueue_job(factory, project_id)
    queue = RenderQueue(settings, factory, encoder=FileEncoder(), prober=prober)
    await queue.start()
    try:
        await asyncio.wait_for(queue.wait_idle(), 30.0)
    finally:
        await queue.stop()
    return job_id


# ---------------------------------------------------------------------------
# Encode-time probe
# ---------------------------------------------------------------------------


async def test_finished_render_stores_probed_codec(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    prober = RecordingProber()
    job_id = await _run_one(migrated_factory, settings_no_autostart, prober)

    assert _codec(migrated_factory, job_id) == ("done", "av01.0.05M.08", "video/mp4")
    assert len(prober.paths) == 1 and prober.paths[0].is_file()


async def test_failed_probe_leaves_null_and_render_done(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    job_id = await _run_one(
        migrated_factory, settings_no_autostart, RecordingProber(answer=None)
    )
    assert _codec(migrated_factory, job_id) == ("done", None, None)


async def test_probe_exception_never_fails_the_render(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    prober = RecordingProber(error=RuntimeError("ffprobe exploded"))
    job_id = await _run_one(migrated_factory, settings_no_autostart, prober)

    assert _codec(migrated_factory, job_id) == ("done", None, None)
    assert len(prober.paths) == 1  # it was tried, and its failure contained


# ---------------------------------------------------------------------------
# Startup backfill (D14)
# ---------------------------------------------------------------------------


def _seed_done(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    project_id: int,
    name: str,
    *,
    kind: str = "manual",
    status: str = "done",
    with_file: bool = True,
    codec_string: str | None = None,
    mime_type: str | None = None,
) -> int:
    with session_scope(factory) as session:
        project = session.get(Project, project_id)
        assert project is not None
        root = project_render_root(settings, project)
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        if with_file:
            path.write_bytes(b"\x00" * 64)
        job = RenderJob(
            project_id=project_id,
            kind=kind,
            status=status,
            output_file_path=str(path),
            codec_string=codec_string,
            mime_type=mime_type,
        )
        session.add(job)
        session.flush()
        return job.id


async def test_backfill_probes_only_unprobed_done_renders_with_files(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    s = settings_no_autostart
    pid = _seed_project(migrated_factory, s)
    todo = _seed_done(migrated_factory, s, pid, "old.mp4")
    probed = _seed_done(
        migrated_factory,
        s,
        pid,
        "new.mp4",
        codec_string="avc1.64001F",
        mime_type="video/mp4",
    )
    no_codec = _seed_done(
        migrated_factory, s, pid, "odd.mkv", mime_type="video/x-matroska"
    )
    missing = _seed_done(migrated_factory, s, pid, "gone.mp4", with_file=False)
    export = _seed_done(migrated_factory, s, pid, "export.zip", kind="export")
    failed = _seed_done(migrated_factory, s, pid, "failed.mp4", status="failed")
    prober = RecordingProber()
    queue = RenderQueue(s, migrated_factory, encoder=FileEncoder(), prober=prober)

    assert await queue.backfill_codecs() == 1

    assert [p.name for p in prober.paths] == ["old.mp4"]
    assert _codec(migrated_factory, todo) == ("done", "av01.0.05M.08", "video/mp4")
    assert _codec(migrated_factory, probed)[1] == "avc1.64001F"
    assert _codec(migrated_factory, no_codec)[1:] == (None, "video/x-matroska")
    for untouched in (missing, export, failed):
        assert _codec(migrated_factory, untouched)[1:] == (None, None)

    # Idempotent: a second run finds nothing left to do.
    assert await queue.backfill_codecs() == 0
    assert len(prober.paths) == 1


async def test_backfill_is_never_fatal(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    s = settings_no_autostart
    pid = _seed_project(migrated_factory, s)
    first = _seed_done(migrated_factory, s, pid, "a.mp4")
    second = _seed_done(migrated_factory, s, pid, "b.mp4")
    prober = RecordingProber(error=RuntimeError("boom"))
    queue = RenderQueue(s, migrated_factory, encoder=FileEncoder(), prober=prober)

    assert await queue.backfill_codecs() == 0

    # Every candidate was still tried, one at a time, and left NULL to retry.
    assert [p.name for p in prober.paths] == ["a.mp4", "b.mp4"]
    for job_id in (first, second):
        assert _codec(migrated_factory, job_id)[1:] == (None, None)


async def test_stop_cancels_the_pending_backfill(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    prober = RecordingProber()
    queue = RenderQueue(
        settings_no_autostart, migrated_factory, encoder=FileEncoder(), prober=prober
    )
    await queue.start()
    await asyncio.wait_for(queue.wait_idle(), 30.0)
    await asyncio.wait_for(queue.stop(), 5.0)
    assert prober.paths == []
