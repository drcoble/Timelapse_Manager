"""Deleting a camera with JavaScript turned off (#70 F-R9).

The row's Delete form uses ``hx-delete`` when JavaScript runs. Without it the
browser submits the form's plain ``action``; that used to be ``POST
/cameras/{id}`` (no such route), so the page ended on a raw 405 JSON body. The
action is now ``POST /cameras/{id}/delete``, which redirects back to the cameras
page with a banner. Both outcomes are checked in a JavaScript-disabled context
that shares the signed-in session:

* an unused camera is deleted and the page says "Camera deleted.";
* the camera a project uses stays, with the in-use banner instead of JSON.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import _boot_app, _seed_demo_data, login, stop_app

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_USED_CAMERA = 1  # _seed_demo_data's camera, used by project 1
_UNUSED_CAMERA = 2


def _seed_with_unused_camera(db_url: str, data_dir: Path) -> None:
    """The demo data (camera 1 used by project 1) plus an unused camera 2."""
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.models import Camera
    from timelapse_manager.db.session import create_session_factory, session_scope

    _seed_demo_data(db_url, data_dir)
    engine = create_db_engine(db_url)
    try:
        with session_scope(create_session_factory(engine)) as db:
            db.add(Camera(name="unused-cam", address="10.0.0.12", protocol="vapix"))
    finally:
        engine.dispose()


@pytest.fixture()
def nojs_page(
    tmp_path: Path,
    _chromium_page: object,
    browser_context_factory: Callable[..., Any],
) -> Generator[tuple[Any, str], None, None]:
    """A JavaScript-disabled page signed in as admin; yields ``(page, base_url)``."""
    proc, base_url = _boot_app(tmp_path, extra_seed=_seed_with_unused_camera)
    try:
        login(_chromium_page, base_url)
        context = browser_context_factory(java_script_enabled=False)
        page = context.new_page()
        page.set_default_timeout(_TIMEOUT)
        yield page, base_url
    finally:
        stop_app(proc)


def _delete_row(page: Any, base: str, camera_id: int) -> None:
    """Click the row's Delete, then confirm on the page it opens (#123)."""
    _open_confirm_page(page, base, camera_id)
    with page.expect_navigation(wait_until="domcontentloaded"):
        page.locator("button.btn-danger", has_text="Yes, delete").click()


def _open_confirm_page(page: Any, base: str, camera_id: int) -> None:
    page.goto(f"{base}/cameras", wait_until="domcontentloaded")
    row = page.locator(f"#camera-row-{camera_id}")
    # The actions popover is CSS-only (:focus-within), so it opens without JS.
    row.locator(".row-actions-trigger").click()
    with page.expect_navigation(wait_until="domcontentloaded"):
        row.locator("button.danger", has_text="Delete").click()


class TestNoJsCameraDelete:
    def test_delete_asks_first_and_cancel_keeps_the_camera(
        self, nojs_page: tuple[Any, str]
    ) -> None:
        page, base = nojs_page
        _open_confirm_page(page, base, _UNUSED_CAMERA)

        assert page.url.split("?")[0] == (
            f"{base}/cameras/{_UNUSED_CAMERA}/delete-confirm"
        )
        page.get_by_text("This cannot be undone.").wait_for()
        with page.expect_navigation(wait_until="domcontentloaded"):
            page.get_by_role("link", name="Cancel").click()
        assert page.url == f"{base}/cameras"
        assert page.locator(f"#camera-row-{_UNUSED_CAMERA}").count() == 1

    def test_unused_camera_is_deleted_with_a_banner(
        self, nojs_page: tuple[Any, str]
    ) -> None:
        page, base = nojs_page
        _delete_row(page, base, _UNUSED_CAMERA)

        assert page.url == f"{base}/cameras?deleted=1"
        # JavaScript really is off: the <noscript> scan form is part of the page.
        assert page.locator("#scan-range-noscript").is_visible()
        page.locator(".alert.success", has_text="Camera deleted.").wait_for()
        assert page.locator(f"#camera-row-{_UNUSED_CAMERA}").count() == 0
        assert page.locator(f"#camera-row-{_USED_CAMERA}").count() == 1

    def test_camera_in_use_shows_the_banner_not_json(
        self, nojs_page: tuple[Any, str]
    ) -> None:
        page, base = nojs_page
        _delete_row(page, base, _USED_CAMERA)

        assert page.url == f"{base}/cameras?delete_error=in_use&n=1"
        banner = page.locator(".alert.error", has_text="was not deleted")
        banner.wait_for()
        assert "1 project(s)" in banner.text_content()
        assert page.locator(f"#camera-row-{_USED_CAMERA}").count() == 1
        assert "detail" not in page.locator("body").inner_text()
