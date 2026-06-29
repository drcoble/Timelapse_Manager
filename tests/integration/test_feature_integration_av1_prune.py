"""Cross-feature integration tests for the AV1 encoder and auto-prune features.

These tests cover the interactions *between* units that the per-unit tests do not
exercise together:

1. Render-settings round-trip: a project schedule stored with libsvtav1 / fps=48
   / mkv / auto_prune=False produces the expected OutputSettings and does NOT
   prune older renders when run_post_actions fires.

2. AV1 + MKV argv assertion: _build_argv produces -c:v libsvtav1 and
   -f matroska for an AV1+MKV spec (no subprocess spawn required).

3. AV1 is not browser-streamable: the browser_streamable flag from both the
   allowlist surface and the is_browser_streamable helper is False for any
   AV1 output.

4. suggested_fps web form: a project seeded with a known capture_interval_seconds
   renders the expected fps suggestion buttons on the project-settings page.

5. (slow) A real AV1 encode: a 320x240, 24-frame clip, skipped when the
   installed ffmpeg cannot emit AV1 packets.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.encode.browser_streamable import is_browser_streamable
from timelapse_manager.encode.encoder import (
    FrameRef,
    FrameSequence,
    OutputSettings,
    OverlayConfig,
    RenderSpec,
)
from timelapse_manager.encode.ffmpeg_impl import FfmpegEncoder
from timelapse_manager.render import settings as render_settings
from timelapse_manager.render.post_actions import run_post_actions

# Fixture frames shipped with the test suite — real decodable 64x48 JPEGs.
_FIXTURES = Path(__file__).parent.parent / "fixtures" / "frames"
_FFPROBE = shutil.which("ffprobe") or str(Path.home() / ".local" / "bin" / "ffprobe")


# ---------------------------------------------------------------------------
# Module-level autouse: pin Docker probe so post-action tests are hermetic
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _force_not_under_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin running_under_docker to False so post-action logic runs unconditionally."""
    import timelapse_manager.render.post_actions as _mod

    monkeypatch.setattr(_mod, "running_under_docker", lambda: False)


# ---------------------------------------------------------------------------
# DB seed helpers (local copies — do not import from off-limits test files)
# ---------------------------------------------------------------------------


def _seed_project(
    factory: sessionmaker,  # type: ignore[type-arg]
    settings: Settings,
    *,
    render_schedule: dict | None = None,
    name: str = "fi-proj",
    capture_interval_seconds: int | None = None,
) -> int:
    """Insert a Camera + Project and return the project_id."""
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
            render_schedule=render_schedule,
            capture_interval_seconds=capture_interval_seconds,
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
    """Insert a done render and write a stub output file; return the job id."""
    output_file.write_bytes(b"\x00" * 64)
    with session_scope(factory) as session:
        job = RenderJob(
            project_id=project_id,
            kind=kind,
            status="done",
            output_settings={"fps": 48, "codec": "libsvtav1", "container": "mkv"},
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


def _done_render_ids(
    factory: sessionmaker,  # type: ignore[type-arg]
    project_id: int,
    kind: str,
) -> list[int]:
    """Return the ids of all done renders of the given kind for a project."""
    with session_scope(factory) as session:
        rows = (
            session.query(RenderJob)
            .filter(
                RenderJob.project_id == project_id,
                RenderJob.kind == kind,
                RenderJob.status == "done",
            )
            .all()
        )
        return [r.id for r in rows]


# ---------------------------------------------------------------------------
# Helper: build a minimal RenderSpec with fixture frames
# ---------------------------------------------------------------------------


# Real AV1 encode geometry. Deliberately larger and longer than one GOP: some
# SVT-AV1 builds list ``libsvtav1`` in ``-encoders`` yet write no packets for a
# tiny/short input (e.g. a few 64x48 frames), which fails the muxer with
# "received no packets". A 320x240, 24-frame clip is enough for any conforming
# build to emit packets. The capability probe below uses the same geometry.
_AV1_WIDTH = 320
_AV1_HEIGHT = 240
_AV1_FRAME_COUNT = 24

# The fixture JPEGs shipped with the suite (sorted, deterministic). Cycled when
# more frames than fixtures are requested.
_FIXTURE_FRAMES = sorted(_FIXTURES.glob("frame_*.jpg"))


def _frame_refs(count: int = 3) -> list[FrameRef]:
    base = datetime(2024, 3, 1, 0, tzinfo=UTC)
    available = _FIXTURE_FRAMES or [_FIXTURES / "frame_000.jpg"]
    return [
        FrameRef(
            sequence_index=i,
            capture_timestamp=base + timedelta(hours=i),
            absolute_path=available[i % len(available)],
            width=64,
            height=48,
        )
        for i in range(count)
    ]


def _av1_mkv_spec(output_path: Path, render_root: Path) -> RenderSpec:
    return RenderSpec(
        project_id=1,
        frames=FrameSequence(project_id=1, frames=_frame_refs(_AV1_FRAME_COUNT)),
        output_settings=OutputSettings(
            fps=48,
            width=_AV1_WIDTH,
            height=_AV1_HEIGHT,
            codec="libsvtav1",
            container="mkv",
        ),
        overlay=OverlayConfig(),
        chapters=[],
        deflicker=False,
        output_path=output_path,
        project_render_root=render_root,
    )


# ---------------------------------------------------------------------------
# Test 1: render-settings round-trip
#
# A render_schedule stored with libsvtav1 / fps=48 / mkv / auto_prune=False
# should round-trip through output_settings_from_schedule into the expected
# OutputSettings dict, and run_post_actions with that schedule must NOT prune
# older scheduled renders.
# ---------------------------------------------------------------------------


async def test_av1_render_settings_round_trip_no_prune(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """libsvtav1/48fps/mkv/auto_prune=False round-trips and suppresses auto-prune."""
    schedule = {
        "enabled": True,
        "interval_seconds": 3600,
        "encoder": "libsvtav1",
        "container": "mkv",
        "fps": 48,
        "resolution": "source",
        "auto_prune": False,
    }

    # Verify the settings round-trip produces the expected output_settings dict.
    output = render_settings.output_settings_from_schedule(schedule)
    assert output is not None
    assert output["codec"] == "libsvtav1", (
        f"expected libsvtav1, got {output['codec']!r}"
    )
    assert output["container"] == "mkv", f"expected mkv, got {output['container']!r}"
    assert int(output["fps"]) == 48, f"expected 48, got {output['fps']!r}"
    # "source" resolution → no width/height in output
    assert "width" not in output
    assert "height" not in output

    # Verify auto_prune_enabled returns False for this schedule.
    assert render_settings.auto_prune_enabled(schedule) is False

    # Seed a project with this schedule, add two prior scheduled renders, then
    # fire run_post_actions as "scheduled".  Both prior renders must survive.
    project_id = _seed_project(
        migrated_factory,
        settings_no_autostart,
        render_schedule=schedule,
        name="fi-round-trip",
    )
    root = _render_root_for(migrated_factory, settings_no_autostart, project_id)

    prior_files = [root / f"sched-old-{i}.mkv" for i in range(2)]
    prior_ids = [
        _add_done_render(migrated_factory, project_id, kind="scheduled", output_file=f)
        for f in prior_files
    ]

    # The "new" render that triggers post_actions.
    trigger_file = root / "sched-new.mkv"
    trigger_file.write_bytes(b"\x00" * 64)
    with session_scope(migrated_factory) as session:
        trigger_job = RenderJob(
            project_id=project_id,
            kind="scheduled",
            status="done",
            output_settings=output,
            output_file_path=str(trigger_file),
            completed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(trigger_job)
        session.flush()
        trigger_id = trigger_job.id

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=trigger_id,
        output_path=trigger_file,
        action_specs=[],
        kind="scheduled",
    )

    # auto_prune=False → all three scheduled renders must survive.
    surviving = _done_render_ids(migrated_factory, project_id, "scheduled")
    assert len(surviving) == 3, (
        f"auto_prune=False must not prune any renders, "
        f"but {3 - len(surviving)} were deleted"
    )
    for jid in prior_ids:
        assert jid in surviving, f"prior render {jid} was incorrectly pruned"
    for f in prior_files:
        assert f.is_file(), f"prior render file {f} was incorrectly deleted"


# ---------------------------------------------------------------------------
# Test 2: AV1 + MKV argv assertion
#
# FfmpegEncoder._build_argv must produce -c:v libsvtav1 and -f matroska for
# a libsvtav1 + mkv spec.  No subprocess is spawned.
# ---------------------------------------------------------------------------


def test_av1_mkv_argv_contains_correct_encoder_and_muxer(tmp_path: Path) -> None:
    """_build_argv emits -c:v libsvtav1 and -f matroska for AV1+MKV."""
    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = tmp_path / "out.mkv"

    spec = _av1_mkv_spec(output, render_root)
    encoder = FfmpegEncoder()

    with tempfile.TemporaryDirectory() as work:
        argv = encoder._build_argv(spec, output, render_root.resolve(), Path(work))

    # -c:v must be libsvtav1 (not "av1" or any alias)
    assert "-c:v" in argv
    cv_idx = argv.index("-c:v")
    assert argv[cv_idx + 1] == "libsvtav1", (
        f"expected libsvtav1 after -c:v, got {argv[cv_idx + 1]!r}"
    )

    # -f must be matroska (the ffmpeg muxer name for .mkv)
    # There is a -f concat earlier; find the one that follows -c:v.
    f_idx = argv.index("-f", cv_idx)
    assert argv[f_idx + 1] == "matroska", (
        f"expected matroska after -f, got {argv[f_idx + 1]!r}"
    )

    # -r must equal "48"
    assert "-r" in argv
    r_idx = argv.index("-r")
    assert argv[r_idx + 1] == "48", f"expected fps=48 after -r, got {argv[r_idx + 1]!r}"


# ---------------------------------------------------------------------------
# Test 3: AV1 is not browser-streamable
# ---------------------------------------------------------------------------


def test_av1_mp4_is_not_browser_streamable() -> None:
    """AV1 + MP4 is not browser-streamable (only H.264 + MP4 is)."""
    assert is_browser_streamable("libsvtav1", "mp4") is False
    assert is_browser_streamable("av1", "mp4") is False


def test_av1_mkv_is_not_browser_streamable() -> None:
    """AV1 + MKV is not browser-streamable."""
    assert is_browser_streamable("libsvtav1", "mkv") is False


def test_h264_mp4_is_browser_streamable() -> None:
    """Control: H.264 + MP4 is the one browser-streamable combination."""
    assert is_browser_streamable("h264", "mp4") is True
    assert is_browser_streamable("libx264", "mp4") is True


# ---------------------------------------------------------------------------
# Test 4: suggested_fps web form
#
# A project with capture_interval_seconds=30 (in the 5–60 band) should surface
# the fps suggestion buttons [12, 24, 30] on the project-settings page.
# ---------------------------------------------------------------------------


def test_project_settings_page_shows_fps_suggestions_for_30s_interval(
    admin_client,  # type: ignore[no-untyped-def]
) -> None:
    """Project settings page renders fps suggestion buttons for a 30-second interval.

    capture_interval_seconds=30 falls in the 5–60 range → suggested_fps returns
    (12, 24, 30).  The template emits a <button> per suggestion with text
    "{fps} fps".
    """
    from timelapse_manager.runtime import get_context

    # Verify the pure function first so a template failure has a clear root cause.
    suggestions = render_settings.suggested_fps(30)
    assert list(suggestions) == [12, 24, 30], (
        f"expected [12, 24, 30] for 30s interval, got {suggestions}"
    )

    # Seed via the app's own session factory (avoids tmp_path collision with
    # the web_settings fixture that admin_client depends on).
    ctx = get_context()
    with session_scope(ctx.session_factory) as session:
        cam = Camera(name="fps-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name="fps-test-project",
            lifecycle_state="active",
            operational_status="idle",
            capture_interval_seconds=30,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

    resp = admin_client.get(f"/projects/{project_id}/settings")
    assert resp.status_code == 200, (
        f"GET /projects/{project_id}/settings returned {resp.status_code}"
    )

    body = resp.text
    for fps in (12, 24, 30):
        assert f"{fps} fps" in body, (
            f"expected '{fps} fps' button on settings page for 30s interval, not found"
        )


def test_project_settings_page_shows_slow_fps_suggestions_for_hourly_interval(
    admin_client,  # type: ignore[no-untyped-def]
) -> None:
    """Hourly captures (3600s) surface the slow-motion suggestion set [10, 24, 30].

    3600s exactly is on the boundary of the 60–3600 band → suggested_fps returns
    (10, 24, 30).
    """
    from timelapse_manager.runtime import get_context

    suggestions = render_settings.suggested_fps(3600)
    assert list(suggestions) == [10, 24, 30], (
        f"expected [10, 24, 30] for 3600s interval, got {suggestions}"
    )

    ctx = get_context()
    with session_scope(ctx.session_factory) as session:
        cam = Camera(name="fps-hourly-cam", address="127.0.0.1", protocol="vapix")
        session.add(cam)
        session.flush()
        proj = Project(
            camera_id=cam.id,
            name="fps-hourly-project",
            lifecycle_state="active",
            operational_status="idle",
            capture_interval_seconds=3600,
        )
        session.add(proj)
        session.flush()
        project_id = proj.id

    resp = admin_client.get(f"/projects/{project_id}/settings")
    assert resp.status_code == 200

    body = resp.text
    for fps in (10, 24, 30):
        assert f"{fps} fps" in body, (
            f"expected '{fps} fps' button on settings page for 3600s interval"
        )


# ---------------------------------------------------------------------------
# Test 5: auto_prune=True (default) with AV1 scheduled renders
#
# A scheduled render with default auto_prune (key absent → True) keeps only
# the latest scheduled render and leaves manual renders untouched.
# This focuses on the AV1-codec dimension — the prune mechanics for h264 are
# already exercised in test_post_actions.py.
# ---------------------------------------------------------------------------


async def test_av1_scheduled_render_auto_prune_keeps_only_latest(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
    settings_no_autostart: Settings,
) -> None:
    """AV1 scheduled renders: auto-prune keeps the newest, manual renders untouched."""
    # Schedule without auto_prune key → default True
    schedule = {
        "enabled": True,
        "interval_seconds": 3600,
        "encoder": "libsvtav1",
        "container": "mkv",
        "fps": 48,
        "resolution": "source",
    }
    assert render_settings.auto_prune_enabled(schedule) is True

    project_id = _seed_project(
        migrated_factory,
        settings_no_autostart,
        render_schedule=schedule,
        name="fi-av1-prune",
    )
    root = _render_root_for(migrated_factory, settings_no_autostart, project_id)

    # Two older scheduled AV1 renders (will be pruned).
    old_files = [root / f"sched-old-{i}.mkv" for i in range(2)]
    old_ids = [
        _add_done_render(migrated_factory, project_id, kind="scheduled", output_file=f)
        for f in old_files
    ]

    # One manual render (must survive regardless of auto-prune).
    manual_file = root / "manual.mkv"
    manual_id = _add_done_render(
        migrated_factory, project_id, kind="manual", output_file=manual_file
    )

    # The new scheduled render that triggers auto-prune.
    new_file = root / "sched-new.mkv"
    new_file.write_bytes(b"\x00" * 64)
    with session_scope(migrated_factory) as session:
        new_job = RenderJob(
            project_id=project_id,
            kind="scheduled",
            status="done",
            output_settings={"fps": 48, "codec": "libsvtav1", "container": "mkv"},
            output_file_path=str(new_file),
            completed_at=datetime.now(UTC).replace(tzinfo=None),
        )
        session.add(new_job)
        session.flush()
        new_id = new_job.id

    await run_post_actions(
        settings_no_autostart,
        migrated_factory,
        job_id=new_id,
        output_path=new_file,
        action_specs=[],
        kind="scheduled",
    )

    # Only the newest scheduled render must survive.
    surviving_scheduled = _done_render_ids(migrated_factory, project_id, "scheduled")
    assert surviving_scheduled == [new_id], (
        f"expected only [{new_id}] to survive, got {surviving_scheduled}"
    )
    for old_id in old_ids:
        assert old_id not in surviving_scheduled, (
            f"old scheduled render {old_id} should have been pruned"
        )
    for f in old_files:
        assert not f.exists(), f"pruned render file {f} should have been deleted"

    # Manual render must be untouched.
    surviving_manual = _done_render_ids(migrated_factory, project_id, "manual")
    assert manual_id in surviving_manual, "manual render must not be auto-pruned"
    assert manual_file.is_file(), "manual render file must not be deleted"


# ---------------------------------------------------------------------------
# Test 6: real AV1 encode (slow — skipped when libsvtav1 not available)
# ---------------------------------------------------------------------------


def _libsvtav1_available() -> bool:
    """Return True only when ffmpeg can actually *emit* AV1 packets.

    Listing ``libsvtav1`` in ``-encoders`` is necessary but not sufficient: some
    builds accept the encoder yet write no packets for small/short inputs. So we
    additionally run a trial encode at the same geometry the test uses (via an
    in-memory lavfi source, no fixtures) and require it to succeed. When the
    build cannot produce AV1 output, the test is skipped rather than failing.
    """
    if not shutil.which("ffmpeg"):
        return False
    if not shutil.which("ffprobe") and not Path(_FFPROBE).exists():
        return False
    try:
        listed = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "libsvtav1" not in listed.stdout:
            return False
        trial = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostdin",
                "-f",
                "lavfi",
                "-i",
                f"color=c=black:s={_AV1_WIDTH}x{_AV1_HEIGHT}:r=48:d=1",
                "-frames:v",
                str(_AV1_FRAME_COUNT),
                "-c:v",
                "libsvtav1",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return trial.returncode == 0
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.slow
async def test_real_av1_encode_produces_valid_mkv(tmp_path: Path) -> None:
    """A real 320x240 AV1+MKV encode succeeds and ffprobe confirms codec=av1.

    Skipped when the installed ffmpeg cannot emit AV1 packets (no libsvtav1, or
    a build that writes nothing for this input).
    """
    if not _libsvtav1_available():
        pytest.skip("ffmpeg cannot emit AV1 packets in this build")

    render_root = tmp_path / "renders"
    render_root.mkdir()
    output = render_root / "test_av1.mkv"

    spec = _av1_mkv_spec(output, render_root)
    encoder = FfmpegEncoder()
    result = await encoder.render(spec)

    assert result.success is True, f"AV1 encode failed: {result.error}"
    assert result.output_path is not None
    assert result.output_path.is_file(), "expected output file to exist"
    assert result.browser_streamable is False, "AV1 must never be browser-streamable"
    assert result.codec == "libsvtav1"
    assert result.container == "mkv"

    # Confirm the output is decodable and uses the av1 codec.
    probe = subprocess.run(
        [
            _FFPROBE,
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_streams",
            str(result.output_path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert probe.returncode == 0, f"ffprobe failed: {probe.stderr}"
    import json

    info = json.loads(probe.stdout)
    codecs = [s.get("codec_name") for s in info.get("streams", [])]
    assert "av1" in codecs, f"expected av1 stream in {codecs}"
