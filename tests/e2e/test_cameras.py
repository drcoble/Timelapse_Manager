"""E2E for the Cameras screen — the inline add-form opens via HTMX.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestCamerasScreen:
    def test_add_camera_form_opens(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        page.click("button:has-text('Add Camera')")  # type: ignore[union-attr]
        # HTMX swaps the form into the container.
        page.wait_for_selector(  # type: ignore[union-attr]
            "#camera-form-container input[name='name']", timeout=30000
        )
        assert (
            page.locator(  # type: ignore[union-attr]
                "#camera-form-container input[name='name']"
            ).count()
            == 1
        )

    def test_scan_button_present(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        # The Meridian scan button renders the query icon.
        assert (
            page.locator(  # type: ignore[union-attr]
                'button:has-text("Scan Network")'
            ).count()
            == 1
        )
