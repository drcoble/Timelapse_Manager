"""The Renders list in a browser: an idle page does not poll; "Load more" (#67 P5).

* With no render pending or encoding, an open /renders page sends no list
  requests for 30 s (driven by Playwright's fake clock). The header's alerts
  poll still fires in the same window, which proves the fake clock drives
  htmx's ``every`` triggers.
* Showing the tab again refreshes the list once, as a passive poll; the server
  answers 286 and the swapped-in list still has no ``every`` trigger.
* "Load more" appends the next older page below the polled list, and a refresh
  of the list leaves those rows in place.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from tests.e2e.conftest import _boot_app, login, stop_app

pytestmark = pytest.mark.ui

_RENDER_COUNT = 53  # one full page of 50 plus three older renders
_LIST_PATH = "/partials/renders/list"
_FIRE_VISIBLE = "document.dispatchEvent(new Event('tlm:visible'))"


def _seed_done_renders(db_url: str, data_dir: Path) -> None:
    """Seed project 1 with ``_RENDER_COUNT`` finished renders (no files)."""
    from datetime import datetime

    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import Camera, Project, RenderJob
    from timelapse_manager.db.session import create_session_factory, session_scope

    del data_dir  # the rows need no output files
    engine = create_db_engine(db_url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as db:
            cam = Camera(name="list-cam", address="10.0.0.41", protocol="vapix")
            db.add(cam)
            db.flush()
            proj = Project(
                camera_id=cam.id,
                name="List Project",
                capture_interval_seconds=300,
                lifecycle_state="active",
            )
            db.add(proj)
            db.flush()
            for _ in range(_RENDER_COUNT):
                db.add(
                    RenderJob(
                        project_id=proj.id,
                        kind="manual",
                        status="done",
                        output_settings={"fps": 24, "codec": "h264"},
                        started_at=datetime(2026, 10, 1, 12, 0),
                        completed_at=datetime(2026, 10, 1, 12, 2),
                    )
                )
            db.flush()
    finally:
        engine.dispose()


@pytest.fixture()
def renders_page(
    _chromium_page: Any, tmp_path: Path
) -> Generator[tuple[Any, str], None, None]:
    """A signed-in page on a server with only finished renders (nothing active)."""
    proc, base_url = _boot_app(tmp_path, extra_seed=_seed_done_renders)
    try:
        login(_chromium_page, base_url)
        yield _chromium_page, base_url
    finally:
        stop_app(proc)


def _path_is(request: Any, path: str) -> bool:
    """Whether ``request`` is for exactly ``path``."""
    return urlsplit(request.url).path == path


def test_idle_page_sends_no_list_polls(renders_page: tuple[Any, str]) -> None:
    page, base_url = renders_page
    page.clock.install()
    seen: list[Any] = []
    page.on("request", lambda req: seen.append(req))
    page.goto(f"{base_url}/renders")
    assert page.get_attribute("#renders-list", "hx-trigger") == (
        "tlm:visible from:document"
    )

    page.clock.run_for(30_000)
    page.wait_for_timeout(500)
    assert [r.url for r in seen if _path_is(r, _LIST_PATH)] == []
    # Control: the same fake 30 s fired the header's `every 30s` alerts poll.
    assert any(_path_is(r, "/alerts/summary") for r in seen)

    # Showing the tab again refreshes the list once, passively, and the server
    # tells htmx to stop polling.
    with page.expect_response(lambda r: _path_is(r, _LIST_PATH)) as info:
        page.evaluate(_FIRE_VISIBLE)
    assert info.value.status == 286
    assert info.value.request.headers.get("x-tlm-passive") == "1"
    page.clock.run_for(1_000)  # let htmx settle the swap under the fake clock
    page.wait_for_timeout(300)
    assert page.locator("#renders-list").count() == 1
    assert page.get_attribute("#renders-list", "hx-trigger") == (
        "tlm:visible from:document"
    )

    seen.clear()
    page.clock.run_for(30_000)
    page.wait_for_timeout(500)
    assert [r.url for r in seen if _path_is(r, _LIST_PATH)] == []


def test_load_more_appends_older_renders_outside_the_poll(
    renders_page: tuple[Any, str],
) -> None:
    page, base_url = renders_page
    page.goto(f"{base_url}/renders")
    assert page.locator("#renders-list .job-item").count() == 50
    # Newest first: ids 53..4 on the first page.
    assert page.locator("#render-row-53").count() == 1
    assert page.locator("#render-row-3").count() == 0

    with page.expect_response(lambda r: _path_is(r, _LIST_PATH)) as info:
        page.click("#renders-more a")
    assert info.value.status == 200
    page.wait_for_selector("#render-row-1", state="attached")
    assert page.locator("#renders-older .job-item").count() == 3
    # The start of the queue: no further "Load more".
    assert page.locator("#renders-more").count() == 0

    # A refresh of the polled list leaves the older rows where they are.
    with page.expect_response(lambda r: _path_is(r, _LIST_PATH)):
        page.evaluate(_FIRE_VISIBLE)
    page.wait_for_timeout(300)
    assert page.locator("#renders-list .job-item").count() == 50
    assert page.locator("#renders-older .job-item").count() == 3
    assert page.locator("#renders-list").count() == 1

    # A row from the older page opens its card like any other.
    page.click("#render-open-2")
    page.wait_for_selector(
        "#job-card-modal[aria-hidden='false'] .job-card", timeout=30000
    )
    assert "Render #2" in page.inner_text("#job-card-title")
