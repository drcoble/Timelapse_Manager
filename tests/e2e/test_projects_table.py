"""E2E for the compact projects management table.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses the seeded ``logged_in_data_page`` (Camera + Project id 1).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestProjectsTable:
    def test_table_renders_with_open_link(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects")  # type: ignore[union-attr]
        # The compact table is present with its six-column header row.
        assert page.locator("table.projects-table").is_visible()  # type: ignore[union-attr]
        for header in ("Name", "Camera", "Status", "Actions"):
            assert page.locator(  # type: ignore[union-attr]
                f"th:has-text('{header}')"
            ).first.is_visible()
        # The first project row exposes an Open link to its detail page.
        open_link = page.locator(  # type: ignore[union-attr]
            "table.projects-table a:has-text('Open')"
        ).first
        assert open_link.is_visible()
        assert "/projects/" in (open_link.get_attribute("href") or "")
