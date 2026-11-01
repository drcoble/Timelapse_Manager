"""E2E for the New Project form: interval chip sets value + pre-flight shows.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (seeds a Camera so the create form renders).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestNewProject:
    def test_interval_chip_sets_value_and_preflight(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects/new")  # type: ignore[union-attr]
        page.wait_for_selector("#capture_interval_value", timeout=30000)  # type: ignore[union-attr]
        page.click(".chip-group .chip:has-text('5m')")  # type: ignore[union-attr]
        assert page.input_value("#capture_interval_value") == "5"  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#preflight-container .preflight-banner", timeout=30000
        )

    def test_solar_mode_anchor_row_shows_preflight(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        # #22 / D13: an anchor-only (solar / scheduled-times) form gets an
        # estimate too -- the container sits outside the interval-only fields
        # and the anchor rows are sent with the request.
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects/new")  # type: ignore[union-attr]
        page.wait_for_selector("#capture_interval_value", timeout=30000)  # type: ignore[union-attr]
        page.check("input[name='capture_mode'][value='solar']")  # type: ignore[union-attr]
        row = "#exact-time-rows .exact-time-row"
        page.wait_for_selector(row, timeout=30000)  # type: ignore[union-attr]
        # The auto-added row may be solar noon; the seeded camera has no
        # coordinates, so use a clock time.
        page.select_option(f"{row} select[name='anchor_kind']", "clock")  # type: ignore[union-attr]
        page.fill(f"{row} input[name='anchor_time']", "12:00")  # type: ignore[union-attr]
        page.press(f"{row} input[name='anchor_time']", "Tab")  # type: ignore[union-attr]
        banner = page.wait_for_selector(  # type: ignore[union-attr]
            "#preflight-container .preflight-banner", timeout=30000
        )
        assert "512.0 KB/day" in banner.inner_text()  # type: ignore[union-attr]
        # Removing the only capture time clears the estimate.
        page.click(f"{row} button")  # type: ignore[union-attr]
        page.wait_for_function(  # type: ignore[union-attr]
            "() => !document.querySelector('#preflight-container .preflight-banner')",
            timeout=30000,
        )
