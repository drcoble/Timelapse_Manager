"""E2E for the continuous-scroll frames grid: sentinel appends older frames.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (Project id 1 seeded with 65 frames).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestFramesScroll:
    def test_scroll_appends_older_frames(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        page.wait_for_selector(".frame-grid .frame-tile", timeout=30000)  # type: ignore[union-attr]
        # First batch: 60 tiles + a sentinel (65 seeded > 60).
        assert page.locator(".frame-grid .frame-tile").count() == 60  # type: ignore[union-attr]
        assert page.locator(".frame-sentinel").count() == 1  # type: ignore[union-attr]
        # Scroll the sentinel into view -> hx-trigger="revealed" loads the rest.
        page.locator(".frame-sentinel").scroll_into_view_if_needed()  # type: ignore[union-attr]
        page.wait_for_selector(".frame-end-cap", timeout=30000)  # type: ignore[union-attr]
        assert page.locator(".frame-grid .frame-tile").count() == 65  # type: ignore[union-attr]
        assert page.locator(".frame-sentinel").count() == 0  # type: ignore[union-attr]
