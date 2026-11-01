"""Web tests for playing the latest render from the Latest Frame (#24).

* View model: ``_latest_render_view`` picks the newest finished render with a
  file, excluding exports, failed or unfinished renders, renders whose file is
  missing, and other projects' renders.
* Markup, three states: a render (play link with the #25 check attributes and
  a separate player), no render ("No render yet", no link), and a viewer
  (who may play too).
"""

from __future__ import annotations

import re
from datetime import datetime

from fastapi.testclient import TestClient

from tests.web.test_render_card import _seed_project
from tests.web.test_render_playback import _AV1, _H264, _H264_MKV, _seed_render
from timelapse_manager.db.models import Frame, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.routers._viewmodels import _latest_render_view


def _seed_frame(project_id: int) -> None:
    with session_scope(get_context().session_factory) as db:
        db.add(
            Frame(
                project_id=project_id,
                sequence_index=1,
                file_path="frame_0001.jpg",
                lifecycle_state="active",
            )
        )


def _set_completed(job_id: int, when: datetime) -> None:
    with session_scope(get_context().session_factory) as db:
        job = db.get(RenderJob, job_id)
        assert job is not None
        job.completed_at = when


def _seed_export(project_id: int) -> int:
    """A finished frame export (shares render_job), newest of all."""
    with session_scope(get_context().session_factory) as db:
        job = RenderJob(
            project_id=project_id,
            kind="export",
            status="done",
            output_settings={"frame_ids": [1]},
            output_file_path="/nonexistent/export.zip",
            completed_at=datetime(2026, 10, 9),
        )
        db.add(job)
        db.flush()
        return job.id


def _latest(project_id: int) -> int | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        project = db.get(Project, project_id)
        assert project is not None
        view = _latest_render_view(db, project, ctx.settings)
        return None if view is None else view.id


class TestLatestRenderChoice:
    def test_newest_done_render_with_a_file(self, admin_client: TestClient) -> None:
        pid = _seed_project("Choice")
        older = _seed_render(pid, settings=_H264, streamable=True)
        newer = _seed_render(pid, settings=_AV1, streamable=False)
        _set_completed(older, datetime(2026, 10, 1))
        _set_completed(newer, datetime(2026, 10, 2))

        assert _latest(pid) == newer

    def test_finish_time_beats_id(self, admin_client: TestClient) -> None:
        pid = _seed_project("Order")
        first = _seed_render(pid, settings=_H264, streamable=True)
        second = _seed_render(pid, settings=_H264, streamable=True)
        _set_completed(first, datetime(2026, 10, 5))
        _set_completed(second, datetime(2026, 10, 3))

        assert _latest(pid) == first

    def test_exports_failed_pending_and_missing_files_are_skipped(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project("Skips")
        good = _seed_render(pid, settings=_H264, streamable=True)
        _set_completed(good, datetime(2026, 10, 1))
        gone = _seed_render(pid, settings=_AV1, streamable=False, with_file=False)
        _set_completed(gone, datetime(2026, 10, 8))
        _seed_render(pid, settings=_AV1, streamable=False, status="failed")
        _seed_render(pid, settings=_AV1, streamable=False, status="pending")
        _seed_export(pid)

        assert _latest(pid) == good

    def test_other_projects_renders_are_excluded(
        self, admin_client: TestClient
    ) -> None:
        mine = _seed_project("Mine")
        other = _seed_project("Other")
        _seed_render(other, settings=_H264, streamable=True)

        assert _latest(mine) is None

    def test_no_render_is_none(self, admin_client: TestClient) -> None:
        assert _latest(_seed_project("Empty")) is None


class TestLatestFrameMarkup:
    def test_with_a_render_the_frame_is_a_play_link(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project("Playable")
        _seed_frame(pid)
        job_id = _seed_render(
            pid, settings=_AV1, streamable=False, codec_string="av01.0.05M.08"
        )

        html = admin_client.get(f"/projects/{pid}").text

        link = re.search(r'<a class="latest-frame-play"[^>]*>', html)
        assert link is not None
        tag = link.group(0)
        assert f'href="/renders/{job_id}/stream"' in tag  # no JS: opens the file
        assert 'aria-label="Play latest render"' in tag
        assert 'data-player="#latest-render-player"' in tag
        assert 'data-codec="av01.0.05M.08"' in tag and 'data-mime="video/mp4"' in tag
        assert 'class="latest-frame-play-icon"' in html
        assert "Latest captured frame for Playable" in html
        assert "No render yet" not in html
        # The player sits outside the frame container, hidden until played,
        # with its caption (card link, Close) and the can't-play fallback.
        container = html.index('id="latest-frame-container"')
        player = html.index('id="latest-render-player"')
        frame_block = html[container:player]
        assert frame_block.count("<div") == frame_block.count("</div>")
        assert re.search(r'id="latest-render-player"[^>]*hidden', html, re.S)
        assert f'hx-get="/renders/{job_id}/card"' in html
        assert "data-latest-render-close" in html
        note = re.search(
            r"<p [^>]*data-latest-render-note hidden>(.*?)</p>", html, re.S
        )
        assert note is not None
        assert "This browser can't play AV1 in MP4;" in note.group(1)
        assert f'href="/renders/{job_id}/download"' in note.group(1)
        assert re.search(
            r'<script src="/static/js/latest-render\.js\?v=[0-9a-f]{12}"></script>',
            html,
        )

    def test_note_names_the_container_for_h264_in_mkv(
        self, admin_client: TestClient
    ) -> None:
        # #36 (rc3 acceptance B18, WebKit): the latest render is H.264 in MKV;
        # the note must not say "can't play H.264" -- MP4 H.264 plays fine.
        pid = _seed_project("Matroska")
        _seed_frame(pid)
        _seed_render(pid, settings=_H264_MKV, streamable=False)

        html = admin_client.get(f"/projects/{pid}").text

        note = re.search(
            r"<p [^>]*data-latest-render-note hidden>(.*?)</p>", html, re.S
        )
        assert note is not None
        assert "This browser can't play H.264 in MKV;" in " ".join(
            note.group(1).split()
        )
        assert 'data-codec-label="H.264 in MKV"' in html

    def test_without_a_render_the_frame_is_a_plain_image(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project("Unrendered")
        _seed_frame(pid)

        html = admin_client.get(f"/projects/{pid}").text

        assert "Latest captured frame for Unrendered" in html
        assert "No render yet" in html
        assert "latest-frame-play" not in html
        assert 'id="latest-render-player"' not in html

    def test_viewer_can_play(self, viewer_client: TestClient) -> None:
        pid = _seed_project("Viewer")
        _seed_frame(pid)
        job_id = _seed_render(pid, settings=_H264, streamable=True)

        html = viewer_client.get(f"/projects/{pid}").text

        assert re.search(
            rf'<a class="latest-frame-play"[^>]*href="/renders/{job_id}/stream"', html
        )
        assert viewer_client.get(f"/renders/{job_id}/stream").status_code == 200

    def test_stale_poll_comment_is_gone(self, admin_client: TestClient) -> None:
        """The page does not claim a 60 s poll it does not have."""
        pid = _seed_project("Comment")
        html = admin_client.get(f"/projects/{pid}").text
        assert "polled every 60s" not in html
        assert 'id="latest-frame-container" hx-' not in html
