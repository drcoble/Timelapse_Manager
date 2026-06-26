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
