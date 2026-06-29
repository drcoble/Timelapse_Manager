"""Web tests for the frame-selection bar scaffolding (Phase 2 selection infra).

The selection bar is client-driven: it renders hidden and is revealed by
selection.js on the first selection. These tests assert the server-rendered
scaffolding is correct and render-neutral:

- ``#frames-action-bar`` is present but ``hidden`` by default (zero selection),
  and carries the count span + Clear control selection.js keys on.
- the dedicated ``#frame-action-status`` polite live region exists (distinct
  from the scroll region ``#frame-load-status``).
- the action-bar partial renders standalone without error.

Seed helpers write directly to the running app's session factory via
``get_context()`` like the other web test files.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_project_with_frame(*, name: str = "Selection Project") -> int:
    """Seed a project with a single captured frame; return the project id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="10.0.0.9", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
        )
        db.add(proj)
        db.flush()
        frame = Frame(
            project_id=proj.id,
            sequence_index=0,
            capture_timestamp=datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
            file_path=f"/frames/{proj.id}/00000000.jpg",
            capture_status="captured",
            origin="captured",
            lifecycle_state="active",
        )
        db.add(frame)
        db.flush()
        return proj.id


def test_action_bar_renders_hidden_by_default(admin_client: TestClient) -> None:
    """The selection bar is present but hidden until a selection exists."""
    pid = _seed_project_with_frame()
    html = admin_client.get(f"/frames?project_id={pid}").text

    # The bar element is present...
    assert 'id="frames-action-bar"' in html
    # ...and starts hidden (render-neutral; JS reveals it on first selection).
    bar_open = html.index('id="frames-action-bar"')
    bar_tag = html[bar_open : html.index(">", bar_open) + 1]
    assert "hidden" in bar_tag

    # The controls selection.js keys on are present.
    assert "selection-bar-count" in html
    assert "data-selection-clear" in html


def test_action_status_live_region_present(admin_client: TestClient) -> None:
    """A dedicated polite live region exists, distinct from the scroll region."""
    pid = _seed_project_with_frame()
    html = admin_client.get(f"/frames?project_id={pid}").text

    assert 'id="frame-action-status"' in html
    assert 'id="frame-load-status"' in html  # the scroll region still present


def test_action_bar_partial_renders_standalone() -> None:
    """The action-bar partial renders without error (e.g. for HTMX swaps)."""
    from timelapse_manager.web.dependencies import templates

    rendered = templates.env.get_template("_partials/frames_action_bar.html").render(
        can_operate=True
    )
    assert 'id="frames-action-bar"' in rendered
    assert "hidden" in rendered
    assert "data-selection-clear" in rendered
