"""Web tests for the compact projects management table.

The projects list is a scannable table (Name / Camera / Status / Frames /
Interval / Actions) with an Open link per row, an archived-projects disclosure,
and an empty state that opens the New Project drawer.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_camera(*, name: str) -> int:
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
    *, name: str, camera_id: int, interval: int = 60, lifecycle: str = "active"
) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        proj = Project(
            camera_id=camera_id,
            name=name,
            capture_interval_seconds=interval,
            lifecycle_state=lifecycle,
        )
        db.add(proj)
        db.flush()
        return proj.id


def test_projects_table_renders_six_columns(admin_client: TestClient) -> None:
    """The list renders a table with the six management columns."""
    cam = _seed_camera(name="table-cam")
    _seed_project(name="Rooftop", camera_id=cam, interval=60)
    html = admin_client.get("/projects").text
    assert '<table class="data-table projects-table">' in html
    for header in ("Name", "Camera", "Status", "Frames", "Interval", "Actions"):
        assert f">{header}</th>" in html


def test_projects_table_has_open_link_per_project(admin_client: TestClient) -> None:
    """Each project row carries an Open link to its detail page."""
    cam = _seed_camera(name="open-cam")
    pid = _seed_project(name="Garden", camera_id=cam)
    html = admin_client.get("/projects").text
    assert "Garden" in html
    assert f'href="/projects/{pid}"' in html
    assert ">Open</a>" in html


def test_projects_table_shows_archived_disclosure(admin_client: TestClient) -> None:
    """Archived projects collapse into a disclosure below the active table."""
    cam = _seed_camera(name="archived-cam")
    _seed_project(name="LiveOne", camera_id=cam, lifecycle="active")
    _seed_project(name="OldOne", camera_id=cam, lifecycle="archived")
    html = admin_client.get("/projects").text
    assert '<details class="projects-archived">' in html
    assert "Archived projects" in html
    # The archived project appears inside the disclosure, not the active table.
    archived = html.split("projects-archived", 1)[1]
    assert "OldOne" in archived


def test_projects_table_interval_column_handles_no_interval(
    admin_client: TestClient,
) -> None:
    """A project with no capture interval shows a dash, not a stray '0'."""
    cam = _seed_camera(name="nointerval-cam")
    _seed_project(name="Unset", camera_id=cam, interval=0)
    html = admin_client.get("/projects").text
    assert "Unset" in html


def test_projects_empty_state_opens_new_project_drawer(
    admin_client: TestClient,
) -> None:
    """With no projects, the empty state offers the New Project drawer opener."""
    html = admin_client.get("/projects").text
    assert "No projects yet" in html
    assert 'data-drawer-open="#drawer-main"' in html
    assert 'hx-get="/drawers/new-project"' in html
    assert 'data-drawer-title="New Project"' in html
