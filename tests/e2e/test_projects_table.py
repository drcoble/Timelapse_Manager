"""E2E for the compact projects management table.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses the seeded ``logged_in_data_page`` (Camera + Project id 1).
"""

from __future__ import annotations

import pytest

from tests.e2e._row_click_support import arm_click_probe, settle, wait_ready

pytestmark = pytest.mark.ui


_ROW = "table.projects-table tbody tr.project-row-clickable"


class TestProjectsTable:
    def test_table_renders_with_name_link(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects")  # type: ignore[union-attr]
        assert page.locator("table.projects-table").is_visible()  # type: ignore[union-attr]
        for header in ("Name", "Camera", "Status"):
            assert page.locator(  # type: ignore[union-attr]
                f"th:has-text('{header}')"
            ).first.is_visible()
        # No Actions column; the name is a real link to the project page (#317).
        assert page.locator("th:has-text('Actions')").count() == 0  # type: ignore[union-attr]
        link = page.locator(f"{_ROW} a.project-open-link").first  # type: ignore[union-attr]
        assert link.is_visible()
        assert "/projects/" in (link.get_attribute("href") or "")
        cursor = page.eval_on_selector(_ROW, "el => getComputedStyle(el).cursor")  # type: ignore[union-attr]
        assert cursor == "pointer"

    def test_clicking_the_camera_cell_opens_the_project(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects")  # type: ignore[union-attr]
        wait_ready(page, _ROW)
        page.locator(f"{_ROW} td.project-cell-camera").first.click()  # type: ignore[union-attr]
        page.wait_for_url("**/projects/*", timeout=30_000)  # type: ignore[union-attr]

    def test_drag_select_does_not_navigate(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects")  # type: ignore[union-attr]
        wait_ready(page, _ROW)
        arm_click_probe(page)
        box = page.locator(f"{_ROW} td.project-cell-camera").first.bounding_box()  # type: ignore[union-attr]
        y = box["y"] + box["height"] / 2
        page.mouse.move(box["x"] + 2, y)  # type: ignore[union-attr]
        page.mouse.down()  # type: ignore[union-attr]
        page.mouse.move(box["x"] + box["width"] - 2, y, steps=8)  # type: ignore[union-attr]
        page.mouse.up()  # type: ignore[union-attr]
        assert page.evaluate("() => String(window.getSelection()).length") > 0  # type: ignore[union-attr]
        settle(page)
        assert page.url.rstrip("/").endswith("/projects")  # type: ignore[union-attr]

    def test_modifier_click_on_the_row_does_not_navigate_this_tab(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """A Shift-click on the row is left alone by the row helper."""
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects")  # type: ignore[union-attr]
        wait_ready(page, _ROW)
        arm_click_probe(page)
        page.locator(f"{_ROW} td.project-cell-camera").first.click(  # type: ignore[union-attr]
            modifiers=["Shift"]
        )
        settle(page)
        assert page.url.rstrip("/").endswith("/projects")  # type: ignore[union-attr]
