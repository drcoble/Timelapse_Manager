"""E2E tests for the bulk action buttons in the frame selection bar.

Render-only: these assert that selecting frames reveals the bulk action buttons
(Exclude / Include / Delete) and that they are absent when nothing is selected.
The actual mutation + summary + undo behaviour is covered by the web tests
(``tests/web/test_frames_bulk_routes.py``); a live bulk POST is deliberately not
clicked here, since a CSRF-checked mutation against the SQLite-backed test server
flakes under contention.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent. Uses
``logged_in_data_page`` (admin -> operator, so the operator-gated buttons render).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000


class TestBulkActionButtons:
    def test_buttons_absent_until_selection(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )

        bar = page.locator("#frames-action-bar")  # type: ignore[union-attr]
        # The bar (and so its buttons) is hidden before any selection.
        assert bar.is_hidden()
        delete_btn = page.locator(  # type: ignore[union-attr]
            '#frames-action-bar [data-bulk-action="delete"]'
        )
        assert delete_btn.is_hidden()

    def test_selection_reveals_bulk_buttons(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )

        labels = page.locator(".frame-tile-select")  # type: ignore[union-attr]
        labels.first.wait_for(timeout=_TIMEOUT)
        labels.nth(0).click()
        labels.nth(1).click()

        bar = page.locator("#frames-action-bar")  # type: ignore[union-attr]
        bar.wait_for(state="visible", timeout=_TIMEOUT)

        # All three operator-gated bulk actions are present and visible.
        for op in ("exclude", "include", "delete"):
            btn = page.locator(  # type: ignore[union-attr]
                f'#frames-action-bar [data-bulk-action="{op}"]'
            )
            btn.wait_for(state="visible", timeout=_TIMEOUT)
            assert btn.count() == 1

    def test_delete_is_two_step_confirm(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """Clicking Delete arms an in-bar confirm rather than firing immediately."""
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )

        labels = page.locator(".frame-tile-select")  # type: ignore[union-attr]
        labels.first.wait_for(timeout=_TIMEOUT)
        labels.nth(0).click()

        delete_btn = page.locator(  # type: ignore[union-attr]
            '#frames-action-bar [data-bulk-action="delete"]'
        )
        delete_btn.wait_for(state="visible", timeout=_TIMEOUT)
        delete_btn.click()

        # The first click arms the confirm: the label changes, no POST is sent,
        # the tile stays selected (no result bar swap).
        page.wait_for_function(  # type: ignore[union-attr]
            "() => /confirm/i.test(document.querySelector("
            "'#frames-action-bar [data-bulk-action=\\\"delete\\\"]').textContent)",
            timeout=_TIMEOUT,
        )
        assert (
            page.locator('.frame-tile[data-selected="true"]').count() >= 1  # type: ignore[union-attr]
        )
