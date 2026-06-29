"""E2E for the bulk export control (render-only).

The Export button appears in the action bar for an operator with a selection.
The async export flow (enqueue → poll → download) is covered by the web-layer
tests; driving it from the browser enqueues a real job and races the test
server's CSRF-on-SQLite, so it is intentionally not exercised here.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (admin → operator).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000


class TestExportControl:
    def test_export_button_present_when_selected(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        # No selection -> the action bar (and its Export button) is hidden.
        assert page.locator("#frames-action-bar").is_hidden()  # type: ignore[union-attr]

        labels = page.locator(".frame-tile-select")  # type: ignore[union-attr]
        labels.first.wait_for(timeout=_TIMEOUT)
        labels.nth(0).click()
        page.locator("#frames-action-bar").wait_for(  # type: ignore[union-attr]
            state="visible", timeout=_TIMEOUT
        )
        assert (
            page.locator("#frames-action-bar [data-export-action]").count()  # type: ignore[union-attr]
            == 1
        )
