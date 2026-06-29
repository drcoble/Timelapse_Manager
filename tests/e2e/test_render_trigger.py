"""E2E for the render-trigger panel: open, fps chip sets value, combo-check.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses the seeded ``logged_in_data_page`` (Project id 1).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestRenderTrigger:
    def test_panel_opens_and_fps_chip(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        # Renders tab → open the trigger panel.
        page.click('.tab-item[data-tab-target="#tab-renders"]')  # type: ignore[union-attr]
        page.click(".render-trigger-details > summary")  # type: ignore[union-attr]
        page.wait_for_selector("#render_fps", timeout=30000)  # type: ignore[union-attr]
        # An fps suggestion chip writes the value into the fps input.
        page.click(".render-trigger-panel .chip-group .chip >> nth=0")  # type: ignore[union-attr]
        assert page.input_value("#render_fps") != ""  # type: ignore[union-attr]

    def test_combo_check_warns_on_bad_pair(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1")  # type: ignore[union-attr]
        page.click('.tab-item[data-tab-target="#tab-renders"]')  # type: ignore[union-attr]
        page.click(".render-trigger-details > summary")  # type: ignore[union-attr]
        page.wait_for_selector("#render_encoder", timeout=30000)  # type: ignore[union-attr]
        # VP9 + MP4 is not muxable -> combo-check warns.
        page.select_option("#render_encoder", "libvpx-vp9")  # type: ignore[union-attr]
        page.select_option("#render_container", "mp4")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#render-combo-warning .alert", timeout=30000
        )
