"""Frame-image serving route and the project view's preview/next-capture fields.

The image route (``GET /projects/{project_id}/frames/{frame_id}/image``) serves
local frame bytes to any authenticated user. These tests cover its happy path
(correct bytes + content type), its scoping and not-found behaviour, the
authentication gate, and the path-containment guard that refuses a frame whose
stored path escapes the project's frame directory. They also check that the
project view derives ``latest_frame_url`` and ``next_capture_at`` from the
latest active frame, and that the frame tile carries a ``thumbnail_url``.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.storage import paths
from timelapse_manager.web import routers

from .thumbnail_support import (
    FAKE_THUMB_BYTES,
    FakeFfmpeg,
    cache_entries,
    install_fake_ffmpeg,
)

_JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32 + b"\xff\xd9"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 24


def _seed_camera(*, name: str = "img-cam") -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        db.add(cam)
        db.flush()
        return cam.id


def _seed_project(
    *, name: str, camera_id: int, interval: int = 60, storage_path: str | None = None
) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = Project(
            camera_id=camera_id,
            name=name,
            capture_interval_seconds=interval,
            lifecycle_state="active",
            storage_path=storage_path,
        )
        db.add(proj)
        db.flush()
        return proj.id


def _write_default_frame(
    project_id: int,
    *,
    sequence_index: int = 1,
    file_name: str = "00000001.jpg",
    data: bytes = _JPEG_BYTES,
    capture_timestamp: datetime.datetime | None = None,
    lifecycle_state: str = "active",
    file_path: str | None = None,
) -> int:
    """Write a real on-disk file under the default per-project dir and a row.

    Returns the new frame id. ``file_path`` defaults to ``file_name`` (the
    relative form a default-layout project stores).
    """
    ctx = get_context()
    frame_dir = paths.frames_root(ctx.settings) / str(project_id)
    frame_dir.mkdir(parents=True, exist_ok=True)
    (frame_dir / file_name).write_bytes(data)
    with session_scope(ctx.session_factory) as db:
        frame = Frame(
            project_id=project_id,
            sequence_index=sequence_index,
            file_path=file_path if file_path is not None else file_name,
            capture_timestamp=capture_timestamp,
            lifecycle_state=lifecycle_state,
        )
        db.add(frame)
        db.flush()
        return frame.id


def _seed_frame_row(project_id: int, *, file_path: str, sequence_index: int = 1) -> int:
    """Insert only a Frame row (no file written) with an arbitrary file_path."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        frame = Frame(
            project_id=project_id,
            sequence_index=sequence_index,
            file_path=file_path,
            lifecycle_state="active",
        )
        db.add(frame)
        db.flush()
        return frame.id


class TestFrameImageRoute:
    def test_serves_bytes_with_jpeg_content_type(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Img Serve", camera_id=cam)
        fid = _write_default_frame(pid)

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/image")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == _JPEG_BYTES

    def test_png_frame_served_with_png_content_type(
        self, admin_client: TestClient
    ) -> None:
        # Uploaded frames may be PNG, so the content type is derived from the
        # on-disk extension rather than hardcoded to JPEG.
        cam = _seed_camera()
        pid = _seed_project(name="Img Png", camera_id=cam)
        fid = _write_default_frame(pid, file_name="00000001.png", data=_PNG_BYTES)

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/image")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == _PNG_BYTES

    def test_unknown_frame_id_is_404(self, admin_client: TestClient) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Img Unknown", camera_id=cam)

        resp = admin_client.get(f"/projects/{pid}/frames/999999/image")

        assert resp.status_code == 404

    def test_frame_in_other_project_is_404(self, admin_client: TestClient) -> None:
        cam = _seed_camera()
        pid_a = _seed_project(name="Img Scope A", camera_id=cam)
        pid_b = _seed_project(name="Img Scope B", camera_id=cam)
        # Frame physically belongs to project A.
        fid = _write_default_frame(pid_a)

        # Requested under project B's scope -> must not be served.
        resp = admin_client.get(f"/projects/{pid_b}/frames/{fid}/image")

        assert resp.status_code == 404

    def test_missing_on_disk_file_is_404(self, admin_client: TestClient) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Img Missing", camera_id=cam)
        # Row points at a default-layout file that was never written.
        fid = _seed_frame_row(pid, file_path="00000001.jpg")

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/image")

        assert resp.status_code == 404

    def test_requires_authentication(self, web_client: TestClient) -> None:
        # web_client is started but NOT logged in: the route must reject it.
        # An admin must exist or first-run gating would intercede; seed one
        # without establishing a session.
        from tests.conftest import seed_admin

        seed_admin(web_client)
        cam = _seed_camera()
        pid = _seed_project(name="Img Auth", camera_id=cam)
        fid = _write_default_frame(pid)

        resp = web_client.get(
            f"/projects/{pid}/frames/{fid}/image", follow_redirects=False
        )

        assert resp.status_code == 401

    def test_viewer_role_may_view(self, viewer_client: TestClient) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Img Viewer", camera_id=cam)
        fid = _write_default_frame(pid)

        resp = viewer_client.get(f"/projects/{pid}/frames/{fid}/image")

        assert resp.status_code == 200
        assert resp.content == _JPEG_BYTES

    def test_path_traversal_is_refused(
        self,
        admin_client: TestClient,
        tmp_path_factory,  # type: ignore[no-untyped-def]
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Img Traversal", camera_id=cam)
        # A secret file outside the frames tree, and a frame row whose absolute
        # stored path points straight at it. resolve_absolute returns the
        # absolute path unchanged, so only the containment guard stops it.
        secret = tmp_path_factory.mktemp("outside") / "secret.txt"
        secret.write_bytes(b"TOP SECRET")
        fid = _seed_frame_row(pid, file_path=str(secret))

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/image")

        assert resp.status_code == 404
        assert b"TOP SECRET" not in resp.content

    def test_traversal_segments_in_relative_path_refused(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Img DotDot", camera_id=cam)
        # A relative stored path that climbs out of the per-project frame dir.
        fid = _seed_frame_row(pid, file_path="../../etc/hosts")

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/image")

        assert resp.status_code == 404

    def test_custom_storage_project_is_served(
        self,
        admin_client: TestClient,
        tmp_path_factory,  # type: ignore[no-untyped-def]
    ) -> None:
        # A project with an explicit storage_path stores frames OUTSIDE the
        # frames root (as absolute paths). The containment boundary is that
        # project's own dir, so a legitimate frame there must still be served.
        store = tmp_path_factory.mktemp("custom-store")
        cam = _seed_camera()
        pid = _seed_project(name="Img Custom", camera_id=cam, storage_path=str(store))
        file_path = store / "00000001.jpg"
        file_path.write_bytes(_JPEG_BYTES)
        fid = _seed_frame_row(pid, file_path=str(file_path))

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/image")

        assert resp.status_code == 200
        assert resp.content == _JPEG_BYTES


class TestProjectViewFields:
    """Exercise ``_project_view`` directly.

    Each test takes ``admin_client`` purely so the app lifespan installs the
    runtime context (the view consults the capture supervisor through it); the
    client itself is unused.
    """

    def test_latest_frame_url_populated_when_frame_exists(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="View Latest", camera_id=cam)
        fid = _write_default_frame(pid, sequence_index=3)

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            project = db.get(Project, pid)
            assert project is not None
            view = routers._project_view(db, project)

        assert view.latest_frame_url == (
            f"/projects/{pid}/frames/{fid}/thumbnail?size=640"
        )

    def test_latest_frame_url_none_when_no_frames(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="View Empty", camera_id=cam)

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            project = db.get(Project, pid)
            assert project is not None
            view = routers._project_view(db, project)

        assert view.latest_frame_url is None
        # An active interval project that has never captured is due now (#5).
        assert view.next_capture_at_raw is not None
        due_in = view.next_capture_at_raw - datetime.datetime.now(datetime.UTC)
        assert abs(due_in.total_seconds()) < 60

    def test_latest_frame_url_uses_highest_sequence(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="View Highest", camera_id=cam)
        _write_default_frame(pid, sequence_index=1, file_name="00000001.jpg")
        newest = _write_default_frame(pid, sequence_index=2, file_name="00000002.jpg")

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            project = db.get(Project, pid)
            assert project is not None
            view = routers._project_view(db, project)

        assert view.latest_frame_url == (
            f"/projects/{pid}/frames/{newest}/thumbnail?size=640"
        )

    def test_soft_deleted_frame_ignored_for_latest(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="View SoftDel", camera_id=cam)
        active = _write_default_frame(pid, sequence_index=1, file_name="00000001.jpg")
        _write_default_frame(
            pid,
            sequence_index=2,
            file_name="00000002.jpg",
            lifecycle_state="soft_deleted",
        )

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            project = db.get(Project, pid)
            assert project is not None
            view = routers._project_view(db, project)

        # The highest-sequence frame is soft-deleted, so the active one wins.
        assert view.latest_frame_url == (
            f"/projects/{pid}/frames/{active}/thumbnail?size=640"
        )

    def test_next_capture_at_is_last_capture_plus_interval(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="View NextCap", camera_id=cam, interval=120)
        ts = datetime.datetime.now(datetime.UTC).replace(
            tzinfo=None, microsecond=0
        ) - datetime.timedelta(seconds=30)
        _write_default_frame(pid, sequence_index=1, capture_timestamp=ts)

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            project = db.get(Project, pid)
            assert project is not None
            view = routers._project_view(db, project)

        # Last capture (from the frame table) + the interval.
        assert view.next_capture_at_raw == (
            ts.replace(tzinfo=datetime.UTC) + datetime.timedelta(seconds=120)
        )

    def test_next_capture_at_none_without_timestamp(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="View NoTs", camera_id=cam, interval=120)
        _write_default_frame(pid, sequence_index=1, capture_timestamp=None)

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            project = db.get(Project, pid)
            assert project is not None
            view = routers._project_view(db, project)

        # A frame without a timestamp is not a known capture: due now (#5).
        assert view.next_capture_at_raw is not None
        due_in = view.next_capture_at_raw - datetime.datetime.now(datetime.UTC)
        assert abs(due_in.total_seconds()) < 60


class TestFrameTileThumbnail:
    def test_frames_page_tile_carries_thumbnail_url(
        self, admin_client: TestClient
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Tile Thumb", camera_id=cam)
        fid = _write_default_frame(pid)

        resp = admin_client.get(f"/frames?project_id={pid}")

        assert resp.status_code == 200
        # The gallery tile now points at the dedicated thumbnail route.
        assert f"/projects/{pid}/frames/{fid}/thumbnail" in resp.text


# Path to a real, decodable fixture JPEG (64x48) ffmpeg can downscale.
_REAL_JPEG = (
    Path(__file__).parent.parent / "fixtures" / "frames" / "frame_000.jpg"
).read_bytes()


class TestFrameThumbnail:
    def test_thumbnail_idor_returns_404(self, admin_client: TestClient) -> None:
        # A frame that does not belong to the path project is not found (no ffmpeg).
        cam = _seed_camera()
        pid_a = _seed_project(name="Th A", camera_id=cam)
        pid_b = _seed_project(name="Th B", camera_id=cam)
        fid = _write_default_frame(pid_a)
        resp = admin_client.get(
            f"/projects/{pid_b}/frames/{fid}/thumbnail", follow_redirects=False
        )
        assert resp.status_code == 404

    def test_thumbnail_unauth_is_401(self, web_client: TestClient) -> None:
        from tests.conftest import seed_admin

        seed_admin(web_client)
        cam = _seed_camera()
        pid = _seed_project(name="Th Auth", camera_id=cam)
        fid = _write_default_frame(pid)
        resp = web_client.get(
            f"/projects/{pid}/frames/{fid}/thumbnail", follow_redirects=False
        )
        assert resp.status_code == 401

    @pytest.mark.slow
    def test_thumbnail_is_downscaled_jpeg(self, admin_client: TestClient) -> None:
        from timelapse_manager.cameras._imageinfo import read_dimensions

        cam = _seed_camera()
        pid = _seed_project(name="Th Real", camera_id=cam)
        fid = _write_default_frame(pid, data=_REAL_JPEG)

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content[:2] == b"\xff\xd8"  # JPEG SOI marker
        dims = read_dimensions(resp.content)
        assert dims is not None and dims[0] == 320  # scaled to 320px wide

    @pytest.mark.slow
    def test_thumbnail_is_cached_on_disk(self, admin_client: TestClient) -> None:
        ctx = get_context()
        cam = _seed_camera()
        pid = _seed_project(name="Th Cache", camera_id=cam)
        fid = _write_default_frame(pid, data=_REAL_JPEG)

        cache_file = paths.thumbnail_cache_dir(ctx.settings, pid) / f"{fid}.jpg"
        assert not cache_file.exists()
        first = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")
        assert first.status_code == 200
        assert cache_file.is_file()  # generated and cached
        # A second request still succeeds (served from cache).
        second = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")
        assert second.status_code == 200


class TestFrameImageContainment:
    """The route uses the shared containment helper (#67 S16)."""

    def test_symlinked_frame_out_of_tree_is_404(
        self,
        admin_client: TestClient,
        tmp_path_factory,  # type: ignore[no-untyped-def]
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Img Symlink", camera_id=cam)
        secret = tmp_path_factory.mktemp("outside-link") / "secret.txt"
        secret.write_bytes(b"TOP SECRET")
        fid = _write_default_frame(pid, file_name="00000001.jpg")
        planted = paths.frames_root(get_context().settings) / str(pid) / "00000001.jpg"
        planted.unlink()
        planted.symlink_to(secret)

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/image")

        assert resp.status_code == 404
        assert b"TOP SECRET" not in resp.content

    def test_relative_frame_from_before_storage_path_change_is_served(
        self,
        admin_client: TestClient,
        tmp_path_factory,  # type: ignore[no-untyped-def]
    ) -> None:
        # Captured under the default layout (relative stored name), then the
        # project got a storage_path: the frame is still in frames_root/<id>.
        cam = _seed_camera()
        pid = _seed_project(name="Img Moved", camera_id=cam)
        fid = _write_default_frame(pid)
        store = tmp_path_factory.mktemp("moved-store")
        with session_scope(get_context().session_factory) as db:
            proj = db.get(Project, pid)
            assert proj is not None
            proj.storage_path = str(store)

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/image")

        assert resp.status_code == 200
        assert resp.content == _JPEG_BYTES


# ---------------------------------------------------------------------------
# Bounded, atomic thumbnail generation (#67 P3). ffmpeg is faked
# (``thumbnail_support``), so these run without a binary.
# ---------------------------------------------------------------------------


def _png_header(width: int, height: int) -> bytes:
    """A PNG signature + IHDR declaring ``width`` x ``height`` (no pixels)."""
    import struct

    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", 13)
        + b"IHDR"
        + struct.pack(">II", width, height)
        + b"\x08\x02\x00\x00\x00"
    )


@pytest.fixture()
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    return install_fake_ffmpeg(monkeypatch)


class TestThumbnailBounds:
    def test_generated_once_then_served_from_cache(
        self, admin_client: TestClient, fake_ffmpeg: FakeFfmpeg
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Tb Cache", camera_id=cam)
        fid = _write_default_frame(pid)

        first = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")
        second = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        assert first.status_code == second.status_code == 200
        assert first.content == second.content == FAKE_THUMB_BYTES
        assert first.headers["cache-control"] == "private, max-age=86400"
        assert len(fake_ffmpeg.calls) == 1
        cache_dir = paths.thumbnail_cache_dir(get_context().settings, pid)
        assert cache_entries(cache_dir) == [f"{fid}.jpg"]

    def test_slots_exhausted_serves_full_image_uncached(
        self,
        admin_client: TestClient,
        fake_ffmpeg: FakeFfmpeg,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import contextlib

        from timelapse_manager.encode import thumbnail

        cam = _seed_camera()
        pid = _seed_project(name="Tb Busy", camera_id=cam)
        fid = _write_default_frame(pid)
        monkeypatch.setattr(thumbnail, "SLOT_TIMEOUT_SECONDS", 0.05)
        limit = get_context().settings.render.thumbnail_concurrency

        with contextlib.ExitStack() as stack:
            for _ in range(limit):
                assert stack.enter_context(thumbnail.generation_slot(limit))
            busy = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        assert busy.status_code == 200
        assert busy.content == _JPEG_BYTES  # the full frame
        assert busy.headers["cache-control"] == "private, no-cache"
        assert fake_ffmpeg.calls == []

        # Once a slot is free the real thumbnail is generated and cached.
        free = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")
        assert free.content == FAKE_THUMB_BYTES
        assert free.headers["cache-control"] == "private, max-age=86400"

    def test_oversized_frame_is_404_not_500(
        self, admin_client: TestClient, fake_ffmpeg: FakeFfmpeg
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Tb Bomb", camera_id=cam)
        fid = _write_default_frame(
            pid, file_name="00000001.png", data=_png_header(50_000, 50_000)
        )

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")
        again = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        assert resp.status_code == again.status_code == 404
        assert fake_ffmpeg.calls == []  # refused on the header, before ffmpeg
        cache_dir = paths.thumbnail_cache_dir(get_context().settings, pid)
        assert cache_entries(cache_dir) == []

    def test_oversized_frame_logged_once(
        self,
        admin_client: TestClient,
        fake_ffmpeg: FakeFfmpeg,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from timelapse_manager.web.routers import frames as frames_router

        # The once-per-frame memory is process-wide; frame ids repeat across
        # tests (a fresh database each), so start this one with it empty.
        monkeypatch.setattr(frames_router, "_REFUSED_THUMBNAIL_FRAMES", set())
        cam = _seed_camera()
        pid = _seed_project(name="Tb Bomb Log", camera_id=cam)
        fid = _write_default_frame(
            pid, file_name="00000001.png", data=_png_header(50_000, 50_000)
        )

        with caplog.at_level("WARNING", logger="timelapse_manager.web.routers.frames"):
            for _ in range(3):
                admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        refused = [r for r in caplog.records if f"frame {fid} " in r.getMessage()]
        assert len(refused) == 1

    @pytest.mark.parametrize("mode", ["partial-then-fail", "empty"])
    def test_failed_generation_leaves_no_file(
        self, admin_client: TestClient, fake_ffmpeg: FakeFfmpeg, mode: str
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name=f"Tb Fail {mode}", camera_id=cam)
        fid = _write_default_frame(pid)
        fake_ffmpeg.mode = mode

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        assert resp.status_code == 200
        assert resp.content == _JPEG_BYTES  # graceful fallback to the frame
        assert resp.headers["cache-control"] == "private, no-cache"
        cache_dir = paths.thumbnail_cache_dir(get_context().settings, pid)
        # Neither a partial thumbnail nor a temporary file is left behind.
        assert cache_entries(cache_dir) == []

    def test_ffmpeg_writes_a_temp_name_beside_the_cache_file(
        self, admin_client: TestClient, fake_ffmpeg: FakeFfmpeg
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Tb Temp", camera_id=cam)
        fid = _write_default_frame(pid)

        admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        out = Path(fake_ffmpeg.calls[0][-1])
        cache_dir = paths.thumbnail_cache_dir(get_context().settings, pid)
        assert out.parent == cache_dir
        assert out.name.startswith(f"{fid}{paths.THUMBNAIL_TEMP_MARKER}")
        # ffmpeg picks its output format from the extension.
        assert out.suffix == ".jpg"

    def test_symlink_planted_in_cache_is_not_served(
        self,
        admin_client: TestClient,
        fake_ffmpeg: FakeFfmpeg,
        tmp_path_factory,  # type: ignore[no-untyped-def]
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Tb Planted", camera_id=cam)
        fid = _write_default_frame(pid)
        secret = tmp_path_factory.mktemp("outside-thumb") / "secret.jpg"
        secret.write_bytes(b"TOP SECRET")
        cache_dir = paths.thumbnail_cache_dir(get_context().settings, pid)
        cache_dir.mkdir(parents=True)
        (cache_dir / f"{fid}.jpg").symlink_to(secret)

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        assert resp.status_code == 200
        assert b"TOP SECRET" not in resp.content
        assert not (cache_dir / f"{fid}.jpg").is_symlink()  # replaced
        assert secret.read_bytes() == b"TOP SECRET"

    def test_cache_dir_symlinked_out_of_root_is_not_written(
        self,
        admin_client: TestClient,
        fake_ffmpeg: FakeFfmpeg,
        tmp_path_factory,  # type: ignore[no-untyped-def]
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Tb Escaped", camera_id=cam)
        fid = _write_default_frame(pid)
        outside = tmp_path_factory.mktemp("outside-cache")
        cache_dir = paths.thumbnail_cache_dir(get_context().settings, pid)
        cache_dir.parent.mkdir(parents=True, exist_ok=True)
        cache_dir.symlink_to(outside, target_is_directory=True)

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        assert resp.status_code == 200
        assert resp.content == _JPEG_BYTES
        assert resp.headers["cache-control"] == "private, no-cache"
        assert fake_ffmpeg.calls == []
        assert list(outside.iterdir()) == []
