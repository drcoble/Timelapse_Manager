"""Web tests for per-browser render playback (#25).

* The view models give every finished render with a file a ``stream_url`` and
  the playability inputs, whatever ``browser_streamable`` says.
* The render card and render rows carry the ``data-*`` attributes
  ``playability.js`` reads, a hidden "can't play" note with the download link,
  and -- for no-JS viewers -- a visible player only for H.264/MP4.
* The web stream route serves non-H.264 renders inline with Range.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.web.test_render_card import _card, _render_root, _seed_project
from timelapse_manager.db.models import RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.routers._viewmodels import _render_view

_AV1 = {
    "codec": "libsvtav1",
    "container": "mp4",
    "fps": 24,
    "width": 1280,
    "height": 720,
}
_H264 = {
    "codec": "libx264",
    "container": "mp4",
    "fps": 24,
    "width": 1280,
    "height": 720,
}
_H264_MKV = {
    "codec": "libx264",
    "container": "mkv",
    "fps": 24,
    "width": 1280,
    "height": 720,
}
_VP9 = {
    "codec": "libvpx-vp9",
    "container": "webm",
    "fps": 24,
    "width": 1280,
    "height": 720,
}


def _seed_render(
    project_id: int,
    *,
    settings: dict[str, Any],
    streamable: bool,
    codec_string: str | None = None,
    mime_type: str | None = None,
    status: str = "done",
    with_file: bool = True,
    size: int = 51_200,
) -> int:
    """Insert a render (and its output file) with the given encode settings."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        job = RenderJob(
            project_id=project_id,
            kind="manual",
            status=status,
            output_settings=dict(settings),
            browser_streamable=streamable,
            codec_string=codec_string,
            mime_type=mime_type,
            frame_count=240,
            started_at=datetime(2026, 10, 1, 12, 0, 0),
            completed_at=datetime(2026, 10, 1, 12, 3, 0) if status == "done" else None,
        )
        db.add(job)
        db.flush()
        job_id = job.id
    if status == "done":
        path = _render_root(project_id) / f"render-{job_id}.{settings['container']}"
        if with_file:
            path.write_bytes(bytes(range(256)) * (size // 256))
        with session_scope(ctx.session_factory) as db:
            row = db.get(RenderJob, job_id)
            assert row is not None
            row.output_file_path = str(path)
    return job_id


def _slot(html: str) -> str:
    """The opening tag of the first playability slot in ``html``."""
    match = re.search(r"<[a-z]+ [^>]*data-playability[^>]*>", html)
    assert match is not None, "no playability slot"
    return match.group(0)


class TestViewModel:
    def test_stream_url_for_every_done_render_with_a_file(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        av1 = _seed_render(pid, settings=_AV1, streamable=False)
        h264 = _seed_render(pid, settings=_H264, streamable=True)
        gone = _seed_render(pid, settings=_AV1, streamable=False, with_file=False)
        pending = _seed_render(pid, settings=_AV1, streamable=False, status="pending")

        with session_scope(get_context().session_factory) as db:
            views = {
                i: _render_view(db.get(RenderJob, i))
                for i in (av1, h264, gone, pending)
            }  # type: ignore[arg-type]

        assert views[av1].stream_url == f"/renders/{av1}/stream"
        assert views[h264].stream_url == f"/renders/{h264}/stream"
        assert views[gone].stream_url is None and views[gone].playback is None
        assert views[pending].stream_url is None
        playback = views[av1].playback
        assert playback is not None
        assert playback.label == "AV1 in MP4"
        assert (playback.width, playback.height, playback.fps) == (1280, 720, 24.0)
        # 51 200 bytes over 240 frames at 24 fps (10 s) = 40 960 bit/s.
        assert playback.bitrate == 40_960

    def test_mime_falls_back_to_the_extension_when_not_probed(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        job_id = _seed_render(pid, settings=_VP9, streamable=False)
        with session_scope(get_context().session_factory) as db:
            view = _render_view(db.get(RenderJob, job_id))  # type: ignore[arg-type]
        assert view.playback is not None
        assert view.playback.codec == ""
        assert view.playback.mime == "video/webm"
        assert view.playback.label == "VP9 in WebM"

    def test_label_names_the_container_of_the_file(
        self, admin_client: TestClient
    ) -> None:
        # #36: H.264 in MKV must not read as "can't play H.264" -- the same
        # browser plays H.264 in MP4. The file on disk names the container.
        pid = _seed_project()
        mkv = _seed_render(pid, settings=_H264_MKV, streamable=False)
        with session_scope(get_context().session_factory) as db:
            row = db.get(RenderJob, mkv)
            assert row is not None
            # Settings that disagree with the file lose to the file.
            row.output_settings = {**_H264_MKV, "container": "mp4"}
            db.flush()
            view = _render_view(row)
        assert view.playback is not None
        assert view.playback.label == "H.264 in MKV"


class TestRenderCard:
    def test_av1_card_is_checked_by_the_browser(self, admin_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_render(
            pid,
            settings=_AV1,
            streamable=False,
            codec_string="av01.0.05M.08",
            mime_type="video/mp4",
        )

        html = _card(admin_client, f"/renders/{job_id}/card")

        slot = _slot(html)
        assert 'data-codec="av01.0.05M.08"' in slot
        assert 'data-mime="video/mp4"' in slot
        assert 'data-codec-label="AV1 in MP4"' in slot
        assert 'data-width="1280"' in slot and 'data-height="720"' in slot
        assert 'data-fps="24"' in slot and 'data-bitrate="' in slot
        assert f'src="/renders/{job_id}/stream"' in html
        # No JS: the AV1 player stays hidden; the browser check reveals it.
        assert re.search(r'class="job-card-play"\s+data-play-ok hidden', html)
        # The can't-play fallback names the codec and links the download.
        note = re.search(r"<p [^>]*data-play-no hidden>(.*?)</p>", html, re.S)
        assert note is not None
        assert "This browser can't play AV1 in MP4;" in note.group(1)
        assert f'href="/renders/{job_id}/download"' in note.group(1)
        # The card's codec row shows the probed string.
        assert "<dt>Codec</dt>" in html and "av01.0.05M.08" in html
        assert "Plays in browser" not in html

    def test_h264_card_shows_the_player_without_js(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        job_id = _seed_render(
            pid, settings=_H264, streamable=True, codec_string="avc1.64001F"
        )

        html = _card(admin_client, f"/renders/{job_id}/card")

        assert re.search(r'class="job-card-play"\s+data-play-ok>', html)
        assert 'data-codec="avc1.64001F"' in _slot(html)

    def test_unprobed_render_says_codec_not_recorded(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project()
        job_id = _seed_render(pid, settings=_VP9, streamable=False)

        html = _card(admin_client, f"/renders/{job_id}/card")

        codec_row = html[html.index("<dt>Codec</dt>") :]
        assert codec_row.index("not recorded") < codec_row.index("</dd>")
        slot = _slot(html)
        assert 'data-codec=""' in slot and 'data-mime="video/webm"' in slot

    def test_h264_mkv_card_names_the_container(self, admin_client: TestClient) -> None:
        # #36, found in WebKit: an H.264/MKV render's note named only H.264.
        pid = _seed_project()
        job_id = _seed_render(
            pid,
            settings=_H264_MKV,
            streamable=False,
            codec_string="avc1.64001F",
            mime_type="video/x-matroska",
        )

        html = _card(admin_client, f"/renders/{job_id}/card")

        assert 'data-codec-label="H.264 in MKV"' in _slot(html)
        note = re.search(r"<p [^>]*data-play-no hidden>(.*?)</p>", html, re.S)
        assert note is not None
        assert "This browser can't play H.264 in MKV;" in note.group(1)
        assert "can't play H.264;" not in html

    def test_missing_file_has_no_player(self, admin_client: TestClient) -> None:
        pid = _seed_project()
        job_id = _seed_render(pid, settings=_AV1, streamable=False, with_file=False)

        html = _card(admin_client, f"/renders/{job_id}/card")

        assert "data-playability" not in html
        assert "/stream" not in html


class TestRenderRows:
    def test_rows_carry_the_check_and_keep_download(
        self, viewer_client: TestClient
    ) -> None:
        pid = _seed_project()
        av1 = _seed_render(
            pid, settings=_AV1, streamable=False, codec_string="av01.0.05M.08"
        )
        h264 = _seed_render(pid, settings=_H264, streamable=True)

        html = viewer_client.get("/renders").text

        # Rows are newest first: the H.264 row, then the AV1 row.
        h264_row = html[
            html.index(f'id="render-row-{h264}"') : html.index(f'id="render-row-{av1}"')
        ]
        av1_row = html[html.index(f'id="render-row-{av1}"') :]
        assert 'data-codec="av01.0.05M.08"' in av1_row
        assert re.search(
            rf'href="/renders/{av1}/stream"[^>]*data-play-ok hidden', av1_row, re.S
        )
        assert "This browser can't play AV1 in MP4." in av1_row
        assert f'href="/renders/{av1}/download"' in av1_row

        assert re.search(
            rf'href="/renders/{h264}/stream"[^>]*data-play-ok>', h264_row, re.S
        )

    def test_mkv_row_note_names_the_container(self, viewer_client: TestClient) -> None:
        pid = _seed_project()
        mkv = _seed_render(pid, settings=_H264_MKV, streamable=False)

        html = viewer_client.get("/renders").text

        row = html[html.index(f'id="render-row-{mkv}"') :]
        note = re.search(r"<span [^>]*data-play-no hidden>(.*?)</span>", row, re.S)
        assert note is not None
        assert note.group(1) == "This browser can't play H.264 in MKV."

    def test_base_page_loads_playability_script(
        self, viewer_client: TestClient
    ) -> None:
        html = viewer_client.get("/renders").text
        assert re.search(
            r'<script src="/static/js/playability\.js\?v=[0-9a-f]{12}"></script>', html
        )
        assert viewer_client.get("/static/js/playability.js").status_code == 200


class TestWebStreamRoute:
    @pytest.mark.parametrize(
        ("settings", "mime"), [(_VP9, "video/webm"), (_AV1, "video/mp4")]
    )
    def test_non_h264_render_streams_with_range(
        self, viewer_client: TestClient, settings: dict[str, Any], mime: str
    ) -> None:
        pid = _seed_project()
        job_id = _seed_render(pid, settings=settings, streamable=False, size=4096)

        resp = viewer_client.get(
            f"/renders/{job_id}/stream", headers={"Range": "bytes=10-19"}
        )

        assert resp.status_code == 206
        assert resp.headers["content-type"].startswith(mime)
        assert resp.content == bytes(range(10, 20))
        assert "attachment" not in resp.headers.get("content-disposition", "")


class TestExportNotServedAsRender:
    """#67 S13: a viewer cannot pull an export's zip through the render routes.

    Exports are operator-only (``/frames/export/{id}/download``); the render
    download and stream routes are open to every role, so they 404 an export.
    """

    @pytest.mark.parametrize("route", ["download", "stream"])
    def test_viewer_gets_404_for_an_export_id(
        self, viewer_client: TestClient, route: str
    ) -> None:
        from tests.web.test_render_card import _seed_job

        pid = _seed_project()
        export_id = _seed_job(pid, kind="export", status="done")

        resp = viewer_client.get(f"/renders/{export_id}/{route}")

        assert resp.status_code == 404
        assert b"PK" not in resp.content[:2]

    @pytest.mark.parametrize("route", ["download", "stream"])
    def test_a_render_still_serves(self, viewer_client: TestClient, route: str) -> None:
        pid = _seed_project()
        job_id = _seed_render(pid, settings=_H264, streamable=True)

        assert viewer_client.get(f"/renders/{job_id}/{route}").status_code == 200
