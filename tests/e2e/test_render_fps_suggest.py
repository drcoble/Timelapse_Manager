"""E2E for the suggested render frame rate on the Render now panel (#301).

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses the seeded ``logged_in_data_page`` (Project id 1, 65 frames 1 minute apart,
so the suggestion is 30 fps while the stored default is 24).
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30000


def _open_panel(page: object, base: str) -> None:
    page.goto(f"{base}/projects/1")  # type: ignore[attr-defined]
    page.click('.tab-item[data-tab-target="#tab-renders"]')  # type: ignore[attr-defined]
    page.click(".render-trigger-details > summary")  # type: ignore[attr-defined]
    page.wait_for_selector("#trigger-render-fps", timeout=_TIMEOUT)  # type: ignore[attr-defined]


def _wait_for_suggestion(page: object) -> None:
    page.wait_for_selector(  # type: ignore[attr-defined]
        "#trigger-fps-suggest [data-suggested-fps]", state="attached", timeout=_TIMEOUT
    )


class TestSuggestedFps:
    def test_panel_prefills_the_suggestion_and_label(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page
        _open_panel(page, base)
        _wait_for_suggestion(page)
        assert page.input_value("#trigger-render-fps") == "30"  # type: ignore[attr-defined]
        label = page.inner_text("#trigger-fps-suggestion-label")  # type: ignore[attr-defined]
        assert label.startswith("Suggested: 30 fps (captures about every 1 minute)")

    def test_typed_value_survives_a_refresh_and_use_suggested_restores(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page
        _open_panel(page, base)
        _wait_for_suggestion(page)
        page.fill("#trigger-render-fps", "17")  # type: ignore[attr-defined]
        # A fresh suggestion arrives (as when the frame range changes); the
        # typed value stays.
        with page.expect_response(lambda r: "suggested-fps" in r.url):  # type: ignore[attr-defined]
            page.evaluate(  # type: ignore[attr-defined]
                """() => htmx.ajax(
                    "GET",
                    document.getElementById("trigger-fps-suggest").getAttribute("hx-get"),
                    {target: "#trigger-fps-suggest", swap: "innerHTML"}
                )"""
            )
        page.wait_for_timeout(500)  # type: ignore[attr-defined]
        assert page.input_value("#trigger-render-fps") == "17"  # type: ignore[attr-defined]
        page.click('[data-action="use-suggested-fps"]')  # type: ignore[attr-defined]
        assert page.input_value("#trigger-render-fps") == "30"  # type: ignore[attr-defined]

    def test_custom_value_is_used_by_the_job(
        self, logged_in_data_page: tuple[object, str], tmp_path: Path
    ) -> None:
        page, base = logged_in_data_page
        _open_panel(page, base)
        _wait_for_suggestion(page)
        page.fill("#trigger-render-fps", "17")  # type: ignore[attr-defined]
        with page.expect_navigation():  # type: ignore[attr-defined]
            page.click("form.render-trigger-panel button[type=submit]")  # type: ignore[attr-defined]
        with closing(sqlite3.connect(tmp_path / "e2e_test.db")) as conn:
            row = conn.execute(
                "SELECT json_extract(output_settings, '$.fps') FROM render_job "
                "WHERE kind = 'manual' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None and float(row[0]) == 17

    def test_auto_checkbox_disables_the_number_on_the_settings_form(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page
        page.goto(f"{base}/projects/1")  # type: ignore[attr-defined]
        page.click('.tab-item[data-tab-target="#tab-settings"]')  # type: ignore[attr-defined]
        page.wait_for_selector(  # type: ignore[attr-defined]
            "#render_fps_auto", state="attached", timeout=_TIMEOUT
        )
        # The render settings sit in a collapsed section.
        page.locator("details:has(#render_fps_auto) > summary").click()  # type: ignore[attr-defined]
        assert page.is_enabled("#render_fps")  # type: ignore[attr-defined]
        page.check("#render_fps_auto")  # type: ignore[attr-defined]
        assert page.is_disabled("#render_fps")  # type: ignore[attr-defined]
        page.uncheck("#render_fps_auto")  # type: ignore[attr-defined]
        assert page.is_enabled("#render_fps")  # type: ignore[attr-defined]
