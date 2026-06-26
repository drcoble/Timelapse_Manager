"""The project-detail page folds Settings and Frames into tabs and confirms
destructive actions inline (no native ``confirm()`` dialog)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_project() -> int:
    """Insert a camera + an active project; return the project id."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="tab-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name="tab-project",
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path="/tmp/tab-project",
        )
        db.add(proj)
        db.flush()
        return proj.id


def test_detail_page_has_four_tabs_and_lazy_settings(
    operator_client: TestClient,
) -> None:
    pid = _seed_project()
    html = operator_client.get(f"/projects/{pid}").text
    for target in ("#tab-status", "#tab-settings", "#tab-frames", "#tab-renders"):
        assert f'data-tab-target="{target}"' in html
    # Settings + Frames panels lazy-load their content.
    assert f'hx-get="/projects/{pid}/settings/form"' in html
    assert f'hx-get="/frames/batch?project_id={pid}"' in html


def test_settings_form_fragment_is_form_not_page(operator_client: TestClient) -> None:
    pid = _seed_project()
    resp = operator_client.get(f"/projects/{pid}/settings/form")
    assert resp.status_code == 200
    assert f'action="/projects/{pid}/settings"' in resp.text
    assert 'name="render_enabled"' in resp.text  # a settings field is present
    assert "<html" not in resp.text  # a fragment, not a full page


def test_settings_form_fragment_is_operator_gated(viewer_client: TestClient) -> None:
    pid = _seed_project()
    resp = viewer_client.get(f"/projects/{pid}/settings/form")
    assert resp.status_code == 403


def test_standalone_settings_page_still_renders_same_form(
    operator_client: TestClient,
) -> None:
    pid = _seed_project()
    resp = operator_client.get(f"/projects/{pid}/settings")
    assert resp.status_code == 200
    assert f'action="/projects/{pid}/settings"' in resp.text
    assert "<html" in resp.text  # full page


def test_detail_page_has_no_native_confirm_dialog(operator_client: TestClient) -> None:
    pid = _seed_project()
    html = operator_client.get(f"/projects/{pid}").text
    assert "confirm(" not in html
    assert f'hx-get="/projects/{pid}/delete-confirm"' in html
    assert f'hx-get="/projects/{pid}/archive-confirm"' in html


def test_delete_confirm_fragment_holds_the_real_post(
    operator_client: TestClient,
) -> None:
    pid = _seed_project()
    resp = operator_client.get(f"/projects/{pid}/delete-confirm")
    assert resp.status_code == 200
    assert "inline-confirm" in resp.text
    assert f'action="/projects/{pid}/delete"' in resp.text


def test_archive_confirm_fragment_holds_the_real_post(
    operator_client: TestClient,
) -> None:
    pid = _seed_project()
    resp = operator_client.get(f"/projects/{pid}/archive-confirm")
    assert resp.status_code == 200
    assert "inline-confirm" in resp.text
    assert f'action="/projects/{pid}/archive"' in resp.text


def test_confirm_cancel_returns_empty(operator_client: TestClient) -> None:
    pid = _seed_project()
    resp = operator_client.get(f"/projects/{pid}/confirm-cancel")
    assert resp.status_code == 200
    assert resp.text.strip() == ""
