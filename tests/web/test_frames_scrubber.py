"""Web tests for the scrubber panel + the ribbon's slider promotion.

Covers the controls-partial relocation (the sticky `.scrubber-panel` wraps the
date-jump + ribbon, single-project only) and the accessibility promotion of the
ribbon wrapper from an aria-hidden pointer aid to a `role="slider"` control.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_project(*, name: str = "Scrubber Project") -> int:
    """Seed a Camera + Project + one frame so the frames page renders fully."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="10.0.0.9", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(camera_id=cam.id, name=name, lifecycle_state="active")
        db.add(proj)
        db.flush()
        pid = proj.id
        db.add(
            Frame(
                project_id=pid,
                sequence_index=0,
                file_path=f"/frames/{pid}/00000000.jpg",
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
        )
        return pid


def test_controls_render_scrubber_panel_single_project(
    admin_client: TestClient,
) -> None:
    """Single-project mode wraps the controls in the sticky scrubber panel."""
    pid = _seed_project()
    html = admin_client.get(f"/frames?project_id={pid}").text
    assert "scrubber-panel" in html
    # The jump-row is reserved for a later phase's buttons but present now.
    assert "scrubber-jump-row" in html
    # Both controls still live inside the panel.
    assert "frame-jump" in html
    assert "frame-ribbon" in html


def test_scrubber_panel_absent_under_all_projects(admin_client: TestClient) -> None:
    """All-Projects has no time axis, so no scrubber panel is rendered."""
    _seed_project()
    html = admin_client.get("/frames").text  # no project_id -> global grid
    assert "scrubber-panel" not in html
    assert "frame-ribbon" not in html


def test_ribbon_wrapper_is_slider_not_aria_hidden(admin_client: TestClient) -> None:
    """The ribbon wrapper is promoted to a focusable slider (WCAG 2.1.1)."""
    pid = _seed_project()
    html = admin_client.get(f"/frames?project_id={pid}").text
    # The wrapper is the control: role=slider + tabindex, no aria-hidden.
    assert 'class="frame-ribbon" role="slider"' in html
    assert 'tabindex="0"' in html
    assert 'class="frame-ribbon" aria-hidden="true"' not in html


def test_ribbon_route_serves_decorative_svg_for_scrubber(
    admin_client: TestClient,
) -> None:
    """The scrubber loads the ribbon with decorative=1, so its SVG is hidden;
    the wrapper (role=slider) carries the accessible name instead."""
    pid = _seed_project()
    resp = admin_client.get(f"/partials/projects/{pid}/ribbon?h=36&decorative=1")
    assert resp.status_code == 200
    body = resp.text
    assert "time-ribbon-svg--interactive" in body
    assert "data-start=" in body and "data-end=" in body
    # Decorative: the redundant role=img is dropped; the SVG is hidden from AT.
    assert 'role="presentation"' in body
    assert 'aria-hidden="true"' in body


def test_shell_loads_scrubber_script(admin_client: TestClient) -> None:
    assert "/static/js/scrubber.js" in admin_client.get("/").text


def test_scrubber_assets_served(anon_client: TestClient) -> None:
    js = anon_client.get("/static/js/scrubber.js")
    assert js.status_code == 200 and "placeViewport" in js.text
    css = anon_client.get("/static/css/components/time-ribbon.css")
    assert css.status_code == 200 and ".scrubber-panel" in css.text
