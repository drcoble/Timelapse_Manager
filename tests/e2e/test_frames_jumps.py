"""E2E for the frame-grid jump controls (Start / Newest / gap steppers).

Navigation is GET-based (a jump swaps a fresh window into #frame-grid), so these
are deterministic. The seeded project has > 60 frames, so the Start jump shows
the series-start end-cap while Newest shows a sentinel (more older frames below).

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (project id 1).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000


class TestFrameJumps:
    def test_start_jump_shows_series_start_end_cap(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        page.locator(  # type: ignore[union-attr]
            '.scrubber-jump-row button[aria-label="Jump to the start of capture"]'
        ).click()
        # The oldest batch carries the series-start end-cap.
        page.locator("#frame-grid .frame-end-cap").wait_for(  # type: ignore[union-attr]
            timeout=_TIMEOUT
        )

    def test_newest_jump_loads_newest_batch(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        # Jump to start first, then back to newest, and confirm the newest batch
        # paginates (a sentinel, not the series-start end-cap).
        page.locator(  # type: ignore[union-attr]
            '.scrubber-jump-row button[aria-label="Jump to the start of capture"]'
        ).click()
        page.locator("#frame-grid .frame-end-cap").wait_for(  # type: ignore[union-attr]
            timeout=_TIMEOUT
        )
        page.locator(  # type: ignore[union-attr]
            '.scrubber-jump-row button[aria-label="Jump to the newest frame"]'
        ).click()
        page.locator("#frame-grid .frame-sentinel").wait_for(  # type: ignore[union-attr]
            timeout=_TIMEOUT
        )

    def test_jump_moves_focus_into_grid(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        page.locator(  # type: ignore[union-attr]
            '.scrubber-jump-row button[aria-label="Jump to the newest frame"]'
        ).click()
        page.locator("#frame-grid .frame-thumb").first.wait_for(  # type: ignore[union-attr]
            timeout=_TIMEOUT
        )
        # After a jump the first thumbnail receives keyboard focus.
        page.wait_for_function(  # type: ignore[union-attr]
            "() => { var a = document.activeElement; "
            "return a && a.classList && a.classList.contains('frame-thumb'); }",
            timeout=_TIMEOUT,
        )
