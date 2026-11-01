"""Web tests for the frame grid's weight controls (#70 F-P4, WP-70-K).

A 60-tile batch must stay light (no per-tile ``<form>`` + CSRF field), and a
scroll session stops appending after ``_MAX_GRID_BATCHES`` batches, ending in a
cap that links further back instead of another sentinel.
"""

from __future__ import annotations

import datetime
import html as html_lib
import re
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.routers import frames as frames_router

# The plan's 60,000-byte target for a 60-tile batch (#198). Measured at ~59.5 KB
# (about 1 KB a tile) with the tile's targets inherited from the grid, no
# indentation and no duplicate labels; it was ~101 KB before.
_BATCH_BYTES_BUDGET = 60_000


def _seed_project(n_frames: int, *, untimed: bool = False) -> int:
    """Seed one project with ``n_frames`` frames; return the project id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="cam-weight", address="127.0.0.1", protocol="http")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name="proj-weight",
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path="/tmp/weight",
        )
        db.add(proj)
        db.flush()
        start = datetime.datetime(2026, 1, 1)
        db.add_all(
            Frame(
                project_id=proj.id,
                sequence_index=i,
                capture_timestamp=None
                if untimed
                else start + datetime.timedelta(minutes=i),
                lifecycle_state="active",
                capture_status="captured",
            )
            for i in range(n_frames)
        )
        return proj.id


def test_batch_body_is_light_and_has_no_per_tile_csrf(
    admin_client: TestClient,
) -> None:
    """60 tiles stay under the byte budget; no tile carries a csrf_token field."""
    pid = _seed_project(120)
    resp = admin_client.get(f"/frames/batch?project_id={pid}")
    assert resp.status_code == 200
    assert len(re.findall(r'<div class="frame-tile[ "]', resp.text)) == 60
    assert len(resp.content) < _BATCH_BYTES_BUDGET
    assert 'name="csrf_token"' not in resp.text
    assert "<form" not in resp.text


def test_tile_actions_are_htmx_buttons(admin_client: TestClient) -> None:
    """Remove is a button that hx-posts to the soft-delete route."""
    pid = _seed_project(1)
    html = admin_client.get(f"/frames/batch?project_id={pid}").text
    match = re.search(
        r'<button[^>]*hx-post="(/projects/\d+/frames/\d+/soft-delete)"[^>]*>', html
    )
    assert match is not None
    assert 'type="button"' in match.group(0)


def test_sentinel_carries_next_batch_number(admin_client: TestClient) -> None:
    """The sentinel's hx-get tells the server which batch it is asking for."""
    pid = _seed_project(200)
    html = admin_client.get(f"/frames/batch?project_id={pid}&batch_no=3").text
    assert "frame-sentinel" in html
    assert "batch_no=4" in html
    # The initial page (no batch_no) asks for batch 2 next.
    first = admin_client.get(f"/frames?project_id={pid}").text
    assert "batch_no=2" in first


def test_tenth_batch_renders_cap_with_valid_at(admin_client: TestClient) -> None:
    """The last allowed batch ends in a cap whose ``at`` is the oldest frame."""
    pid = _seed_project(
        (frames_router._MAX_GRID_BATCHES + 1) * frames_router._FRAMES_PER_PAGE
    )
    html = admin_client.get(
        f"/frames/batch?project_id={pid}&batch_no={frames_router._MAX_GRID_BATCHES}"
    ).text
    assert 'class="frame-sentinel"' not in html
    assert "frame-end-cap" in html
    assert "Jump further back" in html
    href = re.search(r'<a class="frame-end-cap-jump" href="([^"]+)"', html)
    assert href is not None
    query = parse_qs(urlparse(html_lib.unescape(href.group(1))).query)
    assert query["project_id"] == [str(pid)]
    anchor = datetime.datetime.fromisoformat(unquote(query["at"][0]))
    assert anchor.tzinfo is not None
    # The batch holds the newest 60 frames of a 660-frame series (one per
    # minute from 2026-01-01 00:00 UTC), so the oldest loaded one is frame 600.
    assert anchor == datetime.datetime(2026, 1, 1, 10, 0, tzinfo=datetime.UTC)
    # The link resolves: the jump page renders a grid.
    jumped = admin_client.get(html_lib.unescape(href.group(1)))
    assert jumped.status_code == 200
    assert "frame-tile-" in jumped.text


def test_cap_before_the_tenth_batch_is_a_sentinel(admin_client: TestClient) -> None:
    pid = _seed_project(
        (frames_router._MAX_GRID_BATCHES + 1) * frames_router._FRAMES_PER_PAGE
    )
    last_open = frames_router._MAX_GRID_BATCHES - 1
    html = admin_client.get(f"/frames/batch?project_id={pid}&batch_no={last_open}").text
    assert 'class="frame-sentinel"' in html
    assert "Jump further back" not in html


def test_series_start_inside_cap_batch_is_a_plain_end_cap(
    admin_client: TestClient,
) -> None:
    """No older frames: the ordinary beginning marker, never a jump link."""
    pid = _seed_project(10)
    html = admin_client.get(
        f"/frames/batch?project_id={pid}&batch_no={frames_router._MAX_GRID_BATCHES}"
    ).text
    assert "Beginning of this project" in html
    assert "Jump further back" not in html


def test_untimed_oldest_frame_falls_back_to_before_cursor(
    admin_client: TestClient,
) -> None:
    pid = _seed_project(150, untimed=True)
    html = admin_client.get(
        f"/frames/batch?project_id={pid}&batch_no={frames_router._MAX_GRID_BATCHES}"
    ).text
    href = re.search(r'<a class="frame-end-cap-jump" href="([^"]+)"', html)
    assert href is not None
    assert "before=" in href.group(1)
    assert "at=" not in href.group(1)


def test_batch_no_is_validated(admin_client: TestClient) -> None:
    pid = _seed_project(1)
    assert (
        admin_client.get(f"/frames/batch?project_id={pid}&batch_no=0").status_code
        == 422
    )
    assert (
        admin_client.get(f"/frames/batch?project_id={pid}&batch_no=x").status_code
        == 422
    )


def test_global_cap_uses_before_cursor(admin_client: TestClient) -> None:
    """The cross-project grid has no time axis, so its cap pages on ``before``."""
    _seed_project(150)
    html = admin_client.get(
        f"/frames/batch?batch_no={frames_router._MAX_GRID_BATCHES}"
    ).text
    href = re.search(r'<a class="frame-end-cap-jump" href="([^"]+)"', html)
    assert href is not None
    assert href.group(1).startswith("/frames?before=")


def test_tiles_inherit_their_drawer_target_from_the_grid(
    admin_client: TestClient,
) -> None:
    """#198: the thumbnail names no target; the grid sets it once, tiles override."""
    pid = _seed_project(2)
    page = admin_client.get(f"/frames?project_id={pid}").text
    grid = re.search(r'<div class="frame-grid"[^>]*>', page)
    assert grid is not None
    assert 'hx-target="#drawer-main .drawer-body"' in grid.group(0)
    assert 'hx-push-url="false"' in grid.group(0)
    batch = admin_client.get(f"/frames/batch?project_id={pid}").text
    thumb = re.search(r'<a class="frame-thumb"[^>]*>', batch)
    assert thumb is not None
    assert "hx-target" not in thumb.group(0)
    # What does not use the grid's target says so itself.
    assert 'hx-target="closest .frame-tile"' in batch


def test_tile_has_no_info_button_and_page_has_no_meta_panel(
    admin_client: TestClient,
) -> None:
    """#315: the whole card opens the drawer; the Info button and its panel are gone."""
    pid = _seed_project(2)
    batch = admin_client.get(f"/frames/batch?project_id={pid}").text
    assert "Info" not in batch
    assert "/metadata" not in batch
    assert "frame-meta-panel" not in batch
    # The one drawer opener per tile is still there, a real link (no-JS, keyboard,
    # Ctrl/Cmd-click) whose click target the stylesheet stretches over the card.
    opener = re.search(r'<a class="frame-thumb" href="([^"]+)"[^>]*>', batch)
    assert opener is not None
    assert 'data-drawer-open="#drawer-main"' in opener.group(0)
    assert 'hx-get="' + opener.group(1) + '/drawer"' in opener.group(0)
    page = admin_client.get(f"/frames?project_id={pid}").text
    assert "frame-meta-panel" not in page


def test_card_stretch_css_keeps_controls_above_the_overlay() -> None:
    """#315: the thumbnail link covers the tile; select and Remove sit above it."""
    css = (
        Path(frames_router.__file__).parent.parent / "static/css/screens/frames.css"
    ).read_text()
    assert ".frame-tile .frame-thumb::after" in css
    assert re.search(r"\.frame-tile \{[^}]*position: relative", css)
    assert re.search(r"\.frame-tile-actions \{[^}]*z-index: 1", css)
    # The thumb wrap must not be the containing block, or the overlay stops at it.
    assert ".frame-tile-thumb-wrap { position: relative; }" not in css
