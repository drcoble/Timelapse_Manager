"""E2E for project detail: client-side tabs + the time-ribbon.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses the seeded ``logged_in_data_page`` (Camera + Project id 1 + frames).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestProjectDetailTabs:
    def test_tab_switch(self, logged_in_data_page: tuple[object, str]) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        assert page.locator("#tab-status").is_visible()  # type: ignore[union-attr]
        assert not page.locator("#tab-renders").is_visible()  # type: ignore[union-attr]
        page.click('.tab-item[data-tab-target="#tab-renders"]')  # type: ignore[union-attr]
        assert page.locator("#tab-renders").is_visible()  # type: ignore[union-attr]
        assert not page.locator("#tab-status").is_visible()  # type: ignore[union-attr]

    def test_ribbon_loads_on_detail(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            ".time-ribbon-slot .time-ribbon svg", timeout=30000
        )
