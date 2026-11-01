"""The dashboard poll in a real browser (#70 F-P1; WP-70-E2).

* An unchanged dashboard costs one ``/partials/projects`` request per poll,
  answered 204: no swap, no ribbon reload, no card image reload.
* After a new frame only that project's ribbon reloads; every other card keeps
  its loaded ribbon node (``hx-preserve``).
* The poll stays passive (WP-70-E1): ``X-TLM-Passive: 1`` with ``X-TLM-Idle``.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

import datetime
import re
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from tests.e2e.conftest import _boot_app, login, stop_app

pytestmark = pytest.mark.ui

_RIBBON = re.compile(r"^/partials/projects/([0-9]+)/ribbon$")
_IMAGE = re.compile(r"^/projects/[0-9]+/frames/[0-9]+/image$")


def _seed_two_projects(db_url: str, data_dir: Path) -> None:
    """Projects 1 and 2, three frames each.

    The campaigns ended in 2025, so the cards are fixed: no "next capture" of
    *now*, and no time bucket in the ribbon key (an ended campaign's ribbon
    does not move), so neither the card nor the poll version can change on its
    own while the test runs.
    """
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import Camera, Frame, Project, User
    from timelapse_manager.db.session import create_session_factory, session_scope

    engine = create_db_engine(db_url)
    try:
        with session_scope(create_session_factory(engine)) as db:
            # A stored zone: the browser's auto-detect (timezone.js) only fills
            # an empty one, so the zone -- part of the poll version -- cannot
            # change under the test after the first page load.
            for user in db.query(User).all():
                user.viewer_timezone = "America/New_York"
            cam = Camera(name="poll-cam", address="10.0.0.12", protocol="vapix")
            db.add(cam)
            db.flush()
            for n in (1, 2):
                proj = Project(
                    camera_id=cam.id,
                    name=f"Poll {n}",
                    capture_interval_seconds=300,
                    lifecycle_state="active",
                    start_date=datetime.datetime(2025, 1, 1),
                    end_date=datetime.datetime(2025, 12, 31),
                )
                db.add(proj)
                db.flush()
                for i in range(3):
                    db.add(
                        Frame(
                            project_id=proj.id,
                            sequence_index=i,
                            capture_timestamp=datetime.datetime(2026, 1, 1, 0, i),
                            file_path=f"/frames/{proj.id}/{i:08d}.jpg",
                            capture_status="captured",
                            origin="captured",
                            lifecycle_state="active",
                        )
                    )
    finally:
        engine.dispose()


def _add_frame(db_url: str, project_id: int) -> int:
    """Insert a new latest frame for ``project_id`` into the live server's DB."""
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import Frame
    from timelapse_manager.db.session import create_session_factory, session_scope

    engine = create_db_engine(db_url)
    try:
        with session_scope(create_session_factory(engine)) as db:
            frame = Frame(
                project_id=project_id,
                sequence_index=99,
                capture_timestamp=datetime.datetime(2026, 1, 2),
                file_path=f"/frames/{project_id}/00000099.jpg",
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
            db.add(frame)
            db.flush()
            return frame.id
    finally:
        engine.dispose()


@pytest.fixture()
def dashboard(
    tmp_path: Path, _chromium_page: Any
) -> Generator[tuple[Any, str, Callable[[int], int]], None, None]:
    """A signed-in page on a server with two projects, and a frame inserter."""
    proc, base_url = _boot_app(tmp_path, extra_seed=_seed_two_projects)
    db_url = f"sqlite:///{tmp_path / 'e2e_test.db'}"
    try:
        login(_chromium_page, base_url)
        yield _chromium_page, base_url, lambda pid: _add_frame(db_url, pid)
    finally:
        stop_app(proc)


def _path(request: Any) -> str:
    return urlsplit(request.url).path


def _grid_polls(seen: list[Any]) -> list[Any]:
    return [r for r in seen if _path(r) == "/partials/projects"]


def _ribbons(seen: list[Any]) -> list[str]:
    return [m[1] for r in seen if (m := _RIBBON.match(_path(r)))]


def test_unchanged_dashboard_poll_is_one_204_and_reloads_nothing(
    dashboard: tuple[Any, str, Callable[[int], int]],
) -> None:
    page, base_url, add_frame = dashboard
    page.clock.install()
    seen: list[Any] = []
    page.on("request", lambda req: seen.append(req))
    with (
        page.expect_response(lambda r: _path(r) == "/partials/projects/1/ribbon"),
        page.expect_response(lambda r: _path(r) == "/partials/projects/2/ribbon"),
    ):
        page.goto(f"{base_url}/")
    page.wait_for_load_state("networkidle")
    # Mark the loaded ribbon nodes; a preserved node keeps the mark.
    page.evaluate(
        "document.querySelectorAll('.time-ribbon-slot').forEach(e => e.__mark = 1)"
    )

    seen.clear()
    with page.expect_response(lambda r: _path(r) == "/partials/projects") as info:
        page.clock.run_for(30_000)
    assert info.value.status == 204
    page.wait_for_timeout(500)
    polls = _grid_polls(seen)
    assert len(polls) == 1
    headers = polls[0].headers
    # The E1 contract: the grid poll is passive, and now carries its version.
    assert headers.get("x-tlm-passive") == "1"
    assert re.fullmatch(r"[0-9]+", headers.get("x-tlm-idle", ""))
    assert re.fullmatch(r"[0-9a-f]{16}", headers.get("x-tlm-poll-version", ""))
    assert _ribbons(seen) == []
    assert [r.url for r in seen if _IMAGE.match(_path(r))] == []

    # A new frame for project 1: the next poll swaps, and only that ribbon reloads.
    latest = add_frame(1)
    seen.clear()
    with page.expect_response(lambda r: _RIBBON.match(_path(r)) is not None):
        page.clock.run_for(30_000)
    page.wait_for_timeout(500)
    polls = _grid_polls(seen)
    assert len(polls) == 1
    assert _ribbons(seen) == ["1"]
    assert page.locator(f"[id^='ribbon-slot-1-{latest}-']").count() == 1
    marks = dict(
        page.evaluate(
            "[...document.querySelectorAll('.time-ribbon-slot')]"
            ".map(e => [e.id.split('-')[2], e.__mark === 1])"
        )
    )
    # Project 1's slot is new; project 2's is the same, preserved node.
    assert marks == {"1": False, "2": True}

    # The 200 brought a new version, so the poll after it is a 204 again.
    seen.clear()
    with page.expect_response(lambda r: _path(r) == "/partials/projects") as info:
        page.clock.run_for(30_000)
    assert info.value.status == 204


def test_ribbons_loaded_by_a_poll_swap_are_passive_but_clicks_are_not(
    dashboard: tuple[Any, str, Callable[[int], int]],
) -> None:
    """#278: a poll swap's lazy ribbons must not count as user activity."""
    page, base_url, add_frame = dashboard
    page.clock.install()
    with (
        page.expect_response(lambda r: _path(r) == "/partials/projects/1/ribbon"),
        page.expect_response(lambda r: _path(r) == "/partials/projects/2/ribbon"),
    ):
        page.goto(f"{base_url}/")
    page.wait_for_load_state("networkidle")

    add_frame(1)
    seen: list[Any] = []
    page.on("request", lambda req: seen.append(req))
    with page.expect_response(lambda r: _RIBBON.match(_path(r)) is not None):
        page.clock.run_for(30_000)
    ribbons = [r for r in seen if _RIBBON.match(_path(r))]
    assert ribbons, "the poll swap reloaded no ribbon"
    for req in ribbons:
        assert req.headers.get("x-tlm-passive") == "1"
        assert re.fullmatch(r"[0-9]+", req.headers.get("x-tlm-idle", ""))

    # A click on an htmx control inside the grid is an ordinary request.
    page.evaluate(
        """() => {
          const b = document.createElement('button');
          b.type = 'button';
          b.id = 'e2e-card-control';
          b.setAttribute('hx-get', '/partials/projects/1/ribbon?h=20');
          b.setAttribute('hx-swap', 'none');
          document.querySelector('#projects-grid').appendChild(b);
          htmx.process(b);
        }"""
    )
    seen.clear()
    with page.expect_response(lambda r: _RIBBON.match(_path(r)) is not None):
        page.click("#e2e-card-control")
    clicked = [r for r in seen if _RIBBON.match(_path(r))]
    assert len(clicked) == 1
    assert "x-tlm-passive" not in clicked[0].headers
