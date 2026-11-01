"""The 640-px thumbnail for dashboard cards and the project header (#70 F-P2).

``GET /projects/{pid}/frames/{fid}/thumbnail`` takes ``size`` in {320, 640};
320 stays the default and keeps its ``<fid>.jpg`` cache name, 640 caches as
``<fid>_640.jpg``. Both go through the same generation cap and atomic write
(#67 P3). ffmpeg is faked (``thumbnail_support``); only CI with real ffmpeg
proves the real 640-px downscale.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from timelapse_manager.runtime import get_context
from timelapse_manager.storage import paths

from .test_frame_image import _seed_camera, _seed_project, _write_default_frame
from .thumbnail_support import (
    FAKE_THUMB_BYTES,
    FakeFfmpeg,
    cache_entries,
    install_fake_ffmpeg,
)


@pytest.fixture()
def fake_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    return install_fake_ffmpeg(monkeypatch)


def _frame(name: str) -> tuple[int, int]:
    pid = _seed_project(name=name, camera_id=_seed_camera(name=f"{name}-cam"))
    return pid, _write_default_frame(pid)


class TestThumbnailSizes:
    def test_size_640_caches_under_its_own_name(
        self, admin_client: TestClient, fake_ffmpeg: FakeFfmpeg
    ) -> None:
        pid, fid = _frame("Size 640")

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail?size=640")

        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == FAKE_THUMB_BYTES
        assert fake_ffmpeg.scale_widths() == ["scale=640:-1"]
        cache_dir = paths.thumbnail_cache_dir(get_context().settings, pid)
        assert cache_entries(cache_dir) == [f"{fid}_640.jpg"]

    @pytest.mark.parametrize("query", ["", "?size=320"])
    def test_size_320_is_the_unchanged_default(
        self, admin_client: TestClient, fake_ffmpeg: FakeFfmpeg, query: str
    ) -> None:
        pid, fid = _frame(f"Size 320 {query}")

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail{query}")

        assert resp.status_code == 200
        assert fake_ffmpeg.scale_widths() == ["scale=320:-1"]
        cache_dir = paths.thumbnail_cache_dir(get_context().settings, pid)
        assert cache_entries(cache_dir) == [f"{fid}.jpg"]

    def test_sizes_are_cached_independently(
        self, admin_client: TestClient, fake_ffmpeg: FakeFfmpeg
    ) -> None:
        pid, fid = _frame("Size Both")
        base = f"/projects/{pid}/frames/{fid}/thumbnail"

        for url in (base, f"{base}?size=640", base, f"{base}?size=640"):
            assert admin_client.get(url).status_code == 200

        assert fake_ffmpeg.scale_widths() == ["scale=320:-1", "scale=640:-1"]
        cache_dir = paths.thumbnail_cache_dir(get_context().settings, pid)
        assert cache_entries(cache_dir) == [f"{fid}.jpg", f"{fid}_640.jpg"]

    @pytest.mark.parametrize("size", ["999", "0", "-320", "abc", "1e9"])
    def test_other_sizes_are_422(
        self, admin_client: TestClient, fake_ffmpeg: FakeFfmpeg, size: str
    ) -> None:
        pid, fid = _frame(f"Size Bad {size}")

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail?size={size}")

        assert resp.status_code == 422
        assert fake_ffmpeg.calls == []

    def test_size_does_not_bypass_the_idor_check(
        self, admin_client: TestClient, fake_ffmpeg: FakeFfmpeg
    ) -> None:
        pid_a, fid = _frame("Size Idor A")
        pid_b, _ = _frame("Size Idor B")

        resp = admin_client.get(f"/projects/{pid_b}/frames/{fid}/thumbnail?size=640")

        assert resp.status_code == 404
        assert fake_ffmpeg.calls == []
        assert pid_a != pid_b


class TestCardsUseTheMediumThumbnail:
    def test_dashboard_card_links_the_640_thumbnail(
        self, admin_client: TestClient
    ) -> None:
        pid, fid = _frame("Card Medium")

        resp = admin_client.get("/")

        assert resp.status_code == 200
        url = f"/projects/{pid}/frames/{fid}/thumbnail?size=640"
        assert f'src="{url}"' in resp.text
        assert f"/projects/{pid}/frames/{fid}/image" not in resp.text

    def test_project_header_links_the_640_thumbnail(
        self, admin_client: TestClient
    ) -> None:
        pid, fid = _frame("Header Medium")

        resp = admin_client.get(f"/projects/{pid}")

        assert resp.status_code == 200
        assert f"/projects/{pid}/frames/{fid}/thumbnail?size=640" in resp.text
