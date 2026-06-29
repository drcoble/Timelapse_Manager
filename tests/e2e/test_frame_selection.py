"""E2E tests for frame selection (client-side, no server mutation).

Selecting frames toggles a per-tile state and reveals the selection bar with a
live count; clearing hides it. Selecting and inspecting are orthogonal — the
thumbnail still opens the drawer without disturbing the selection. All
interactions are client-side (no POST), so these are deterministic.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (admin → operator, so the select controls render).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_DRAWER_OPEN_JS = (
    "() => document.querySelector('#drawer-main')"
    ".getAttribute('aria-hidden') === 'false'"
)


class TestFrameSelection:
    def test_select_toggles_bar_and_count(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )

        bar = page.locator("#frames-action-bar")  # type: ignore[union-attr]
        # Hidden until something is selected.
        assert bar.is_hidden()

        labels = page.locator(".frame-tile-select")  # type: ignore[union-attr]
        labels.first.wait_for(timeout=_TIMEOUT)
        labels.nth(0).click()

        # Bar appears with a count of 1.
        bar.wait_for(state="visible", timeout=_TIMEOUT)
        count = page.locator(  # type: ignore[union-attr]
            "#frames-action-bar .selection-bar-count"
        )
        page.wait_for_function(  # type: ignore[union-attr]
            "() => /\\b1\\b/.test(document.querySelector("
            "'#frames-action-bar .selection-bar-count').textContent)",
            timeout=_TIMEOUT,
        )
        assert "select" in count.text_content().lower()
        # The tile is marked selected.
        assert (
            page.locator('.frame-tile[data-selected="true"]').count()  # type: ignore[union-attr]
            == 1
        )

        # Selecting a second tile bumps the count to 2.
        labels.nth(1).click()
        page.wait_for_function(  # type: ignore[union-attr]
            "() => /\\b2\\b/.test(document.querySelector("
            "'#frames-action-bar .selection-bar-count').textContent)",
            timeout=_TIMEOUT,
        )

        # Clear hides the bar and deselects.
        page.locator("#frames-action-bar [data-selection-clear]").click()  # type: ignore[union-attr]
        bar.wait_for(state="hidden", timeout=_TIMEOUT)
        assert (
            page.locator('.frame-tile[data-selected="true"]').count() == 0  # type: ignore[union-attr]
        )

    def test_thumbnail_still_opens_drawer_independent_of_selection(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )

        # Select the first tile.
        labels = page.locator(".frame-tile-select")  # type: ignore[union-attr]
        labels.first.wait_for(timeout=_TIMEOUT)
        labels.nth(0).click()
        page.locator(  # type: ignore[union-attr]
            '.frame-tile[data-selected="true"]'
        ).first.wait_for(timeout=_TIMEOUT)

        # Clicking a thumbnail opens the drawer without changing the selection.
        page.locator(  # type: ignore[union-attr]
            'a[hx-get^="/projects/1/frames/"][hx-get$="/drawer"]'
        ).first.click()
        page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]
        assert (
            page.locator('.frame-tile[data-selected="true"]').count() >= 1  # type: ignore[union-attr]
        )
