"""E2E nav smoke — the flattened nav renders and its SVG icons paint.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestNav:
    def test_nav_renders_main_items(self, logged_in_page: object) -> None:
        page = logged_in_page  # type: ignore[assignment]
        items = page.locator(".app-nav .nav-item")  # type: ignore[union-attr]
        # 7 main items for admin + 2 admin items = 9 (admin is the seeded user).
        # Notification settings and the audit log live as tabs (on the Settings
        # and Events pages respectively), not as nav entries.
        assert items.count() == 9

    def test_nav_icon_paints(self, logged_in_page: object) -> None:
        page = logged_in_page  # type: ignore[assignment]
        box = page.locator(  # type: ignore[union-attr]
            '.app-nav .nav-item:first-child use[href="#icon-dashboard"]'
        ).bounding_box()
        assert box is not None and box["width"] > 0 and box["height"] > 0

    def test_no_section_labels(self, logged_in_page: object) -> None:
        page = logged_in_page  # type: ignore[assignment]
        assert page.locator(".nav-section-label").count() == 0  # type: ignore[union-attr]
