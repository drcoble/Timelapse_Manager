"""Web tests for the suggested render frame rate (#301)."""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from tests.web.test_project_manage import (
    _post_edit,
    _project,
    _seed_camera,
    _seed_project,
    _set_render_schedule,
)
from tests.web.test_render_trigger import _latest_job_settings
from timelapse_manager.db.models import Frame
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _add_frames(project_id: int, interval: timedelta, count: int) -> None:
    first = datetime(2026, 1, 1, 12)
    with session_scope(get_context().session_factory) as db:
        for i in range(count):
            db.add(
                Frame(
                    project_id=project_id,
                    sequence_index=i,
                    capture_timestamp=first + interval * i,
                    file_path=f"{i:08d}.jpg",
                    capture_status="captured",
                    origin="captured",
                    lifecycle_state="active",
                )
            )


def _project_with_frames(name: str, interval: timedelta, count: int = 50) -> int:
    cam = _seed_camera(name=f"{name}-cam")
    pid = _seed_project(name=name, camera_id=cam)
    _add_frames(pid, interval, count)
    return pid


def test_fragment_shows_label_and_use_suggested(admin_client: TestClient) -> None:
    pid = _project_with_frames("Fps Day", timedelta(days=1), count=200)
    resp = admin_client.get(f"/projects/{pid}/renders/suggested-fps")
    assert resp.status_code == 200
    assert 'data-suggested-fps="12"' in resp.text
    assert "Suggested: 12 fps (captures about every 1 day)" in resp.text
    assert 'data-action="use-suggested-fps"' in resp.text
    assert "few" not in resp.text.lower() and "short" not in resp.text.lower()


def test_fragment_hints_when_the_video_is_short(admin_client: TestClient) -> None:
    pid = _project_with_frames("Fps Short", timedelta(minutes=5), count=10)
    text = admin_client.get(f"/projects/{pid}/renders/suggested-fps").text
    assert 'data-suggested-fps="30"' in text
    assert "Only 10 frames so far" in " ".join(text.split())


def test_fragment_without_enough_frames(admin_client: TestClient) -> None:
    pid = _project_with_frames("Fps None", timedelta(minutes=5), count=1)
    text = admin_client.get(f"/projects/{pid}/renders/suggested-fps").text
    assert 'data-suggested-fps=""' in text
    assert "use-suggested-fps" not in text


def test_fragment_range_and_errors(admin_client: TestClient) -> None:
    pid = _project_with_frames("Fps Range", timedelta(hours=1))
    ok = admin_client.get(
        f"/projects/{pid}/renders/suggested-fps",
        params={"start": "2026-01-01T12:00:00", "end": "2026-01-01T20:00:00"},
    )
    assert ok.status_code == 200 and "about every 1 hour" in ok.text
    reversed_range = admin_client.get(
        f"/projects/{pid}/renders/suggested-fps",
        params={"start": "2026-01-02T00:00:00", "end": "2026-01-01T00:00:00"},
    )
    assert reversed_range.status_code == 422
    assert admin_client.get("/projects/99999/renders/suggested-fps").status_code == 404


def test_fragment_is_not_for_viewers(viewer_client: TestClient) -> None:
    pid = _project_with_frames("Fps Viewer", timedelta(days=1))
    assert (
        viewer_client.get(f"/projects/{pid}/renders/suggested-fps").status_code == 403
    )


def test_render_form_fetches_the_suggestion_lazily(admin_client: TestClient) -> None:
    pid = _project_with_frames("Fps Panel", timedelta(days=1))
    html = admin_client.get(f"/projects/{pid}").text
    assert f'hx-get="/projects/{pid}/renders/suggested-fps"' in html
    assert 'hx-trigger="toggle from:closest details once"' in html
    assert re.search(
        r'<input[^>]*id="trigger-render-fps"[^>]*data-action="fps-touched"', html
    )
    # No suggestion is computed while rendering the page itself.
    assert "Suggested:" not in html


def test_manual_render_with_a_typed_value_uses_it(admin_client: TestClient) -> None:
    pid = _project_with_frames("Fps Typed", timedelta(days=1))
    csrf = csrf_of(admin_client, f"/projects/{pid}")
    resp = admin_client.post(
        f"/projects/{pid}/renders",
        data={"csrf_token": csrf, "render_fps": "17"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert _latest_job_settings(pid)["fps"] == 17


def test_settings_form_offers_auto_and_saves_the_sentinel(
    admin_client: TestClient,
) -> None:
    cam = _seed_camera(name="fps-auto-cam")
    pid = _seed_project(name="Fps Auto", camera_id=cam)
    form = admin_client.get(f"/projects/{pid}/settings").text
    box = re.search(r'<input[^>]*id="render_fps_auto"[^>]*>', form)
    assert box is not None and "checked" not in box.group(0)

    resp = _post_edit(
        admin_client,
        pid,
        cam,
        render_enabled="on",
        render_frequency="3600",
        render_encoder="libx264",
        render_container="mp4",
        render_fps="30",
        render_fps_auto="on",
        render_resolution="1920x1080",
    )
    assert resp.status_code == 303
    stored = _project(pid).render_schedule
    assert stored["fps"] == "auto"
    assert "fps_auto" not in stored

    again = admin_client.get(f"/projects/{pid}/settings").text
    box = re.search(r'<input[^>]*id="render_fps_auto"[^>]*>', again)
    assert box is not None and "checked" in box.group(0)
    number = re.search(r'<input[^>]*id="render_fps"[^>]*>', again)
    assert number is not None and "disabled" in number.group(0)


def test_unticking_auto_stores_the_typed_number(admin_client: TestClient) -> None:
    cam = _seed_camera(name="fps-unauto-cam")
    pid = _seed_project(name="Fps Unauto", camera_id=cam)
    _set_render_schedule(
        pid, {"enabled": True, "interval_seconds": 3600, "fps": "auto"}
    )
    resp = _post_edit(
        admin_client,
        pid,
        cam,
        render_frequency="3600",
        render_encoder="libx264",
        render_container="mp4",
        render_fps="20",
        render_resolution="1920x1080",
    )
    assert resp.status_code == 303
    assert _project(pid).render_schedule["fps"] == 20


def test_manual_render_on_an_auto_schedule_resolves_the_suggestion(
    admin_client: TestClient,
) -> None:
    pid = _project_with_frames("Fps Auto Manual", timedelta(days=1))
    _set_render_schedule(
        pid,
        {
            "enabled": False,
            "interval_seconds": 3600,
            "encoder": "libx264",
            "container": "mp4",
            "fps": "auto",
            "resolution": "1920x1080",
        },
    )
    csrf = csrf_of(admin_client, f"/projects/{pid}")
    resp = admin_client.post(
        f"/projects/{pid}/renders", data={"csrf_token": csrf}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert _latest_job_settings(pid)["fps"] == 12
