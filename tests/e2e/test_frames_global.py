"""E2E for the cross-project ("All Projects") global frames grid.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (65 seeded frames in one project). The global grid
(bare /frames, no project_id) pages on the frame id; this verifies it renders,
carries the project picker, and appends via the scroll sentinel to the end-cap.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestAllProjectsGrid:
    def test_bare_frames_is_all_projects_grid_with_picker(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames")  # type: ignore[union-attr]
        page.wait_for_selector(".frame-grid .frame-tile", timeout=30000)  # type: ignore[union-attr]
        # Project picker present, defaulting to "All Projects" (empty value).
        assert page.locator("#frame-project-select").count() == 1  # type: ignore[union-attr]
        selected = page.eval_on_selector(  # type: ignore[union-attr]
            "#frame-project-select", "el => el.value"
        )
        assert selected == ""  # All Projects

    def test_global_scroll_appends_to_end_cap(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames")  # type: ignore[union-attr]
        page.wait_for_selector(".frame-grid .frame-tile", timeout=30000)  # type: ignore[union-attr]
        first = page.locator(".frame-grid .frame-tile").count()  # type: ignore[union-attr]
        assert first == 60  # first global batch capped at _FRAMES_PER_PAGE
        # The global sentinel pages on the frame id with NO project_id scope.
        href = page.get_attribute(".frame-sentinel", "hx-get")  # type: ignore[union-attr]
        assert href is not None and "project_id=" not in href
        page.locator(".frame-sentinel").scroll_into_view_if_needed()  # type: ignore[union-attr]
        page.wait_for_selector(".frame-end-cap", timeout=30000)  # type: ignore[union-attr]
        assert page.locator(".frame-grid .frame-tile").count() > first  # type: ignore[union-attr]

    def test_all_projects_shows_hint_not_time_controls(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """All-Projects has no time axis: the scrubber panel is replaced by a
        hint, but per-frame selection still works."""
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames")  # type: ignore[union-attr]
        page.wait_for_selector(".frame-grid .frame-tile", timeout=30000)  # type: ignore[union-attr]
        # Time-axis controls are single-project only; the hint stands in.
        assert page.locator(".frames-allprojects-hint").count() == 1  # type: ignore[union-attr]
        assert page.locator(".scrubber-panel").count() == 0  # type: ignore[union-attr]
        # Selection is cross-project, so tile select controls are still present.
        assert page.locator(".frame-tile-select").count() > 0  # type: ignore[union-attr]

    def test_single_project_shows_controls_not_hint(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """The single-project view is the inverse: scrubber controls, no hint."""
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        page.wait_for_selector(".frame-grid .frame-tile", timeout=30000)  # type: ignore[union-attr]
        assert page.locator(".scrubber-panel").count() == 1  # type: ignore[union-attr]
        assert page.locator(".frames-allprojects-hint").count() == 0  # type: ignore[union-attr]
