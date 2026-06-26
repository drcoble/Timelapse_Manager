"""E2E for the bulk timestamp-offset panel (render-only).

Verifies the inline offset panel reveals on demand and its live preview reacts
to the duration inputs. The actual shift (a POST) is covered by the web-layer
tests — applying it from the browser races the test server's CSRF-on-SQLite and
is intentionally not exercised here.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (admin → operator, so the controls render).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000


class TestOffsetPanel:
    def test_offset_panel_reveals_and_previews(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )

        # Select a frame so the action bar (with the Offset button) appears.
        labels = page.locator(".frame-tile-select")  # type: ignore[union-attr]
        labels.first.wait_for(timeout=_TIMEOUT)
        labels.nth(0).click()
        page.locator("#frames-action-bar").wait_for(  # type: ignore[union-attr]
            state="visible", timeout=_TIMEOUT
        )

        panel = page.locator("#frames-offset-panel")  # type: ignore[union-attr]
        assert panel.is_hidden()

        # Open the panel.
        page.locator("[data-offset-toggle]").click()  # type: ignore[union-attr]
        panel.wait_for(state="visible", timeout=_TIMEOUT)

        # The live preview reacts to the hours input.
        page.locator("#frames-offset-panel [data-offset-h]").fill("1")  # type: ignore[union-attr]
        page.wait_for_function(  # type: ignore[union-attr]
            "() => { var p = document.querySelector("
            "'#frames-offset-panel [data-offset-preview]'); "
            "return p && /1h/.test(p.textContent); }",
            timeout=_TIMEOUT,
        )

        # Cancel collapses it.
        page.locator("[data-offset-cancel]").click()  # type: ignore[union-attr]
        panel.wait_for(state="hidden", timeout=_TIMEOUT)
