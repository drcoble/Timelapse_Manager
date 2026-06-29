"""E2E for the frames-browser scrubber: the viewport-indicator rect that tracks
scroll, the ribbon's `role="slider"` keyboard semantics, and the click->date-jump
pre-fill.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent. The
seeded project (id 1) has 65 frames captured one minute apart starting at
2026-01-01T00:00 UTC.

Navigation here is GET-based (the scrubber drives `?at=` loads), so these are
safer than mutation flows; still, they assert presence / attributes / relative
change rather than exact pixel positions (which are inherently brittle).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestFramesScrubber:
    def test_viewport_rect_appears_and_has_width(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """Scrolling the grid reflects onto a sized viewport rect on the ribbon."""
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        page.wait_for_selector(".frame-grid .frame-tile", timeout=30000)  # type: ignore[union-attr]
        # The ribbon SVG (and its data-start/data-end epoch bounds) loads lazily.
        page.wait_for_selector(".frame-ribbon .time-ribbon", timeout=30000)  # type: ignore[union-attr]
        # Nudge the page so the IntersectionObserver resolves a visible window
        # and frames-nav.js emits the scrubber:viewport event.
        page.mouse.wheel(0, 600)  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            ".ribbon-viewport-rect", state="attached", timeout=10000
        )
        # The rect must span a non-zero slice of the ribbon (a loaded window).
        width = page.evaluate(  # type: ignore[union-attr]
            """() => {
                const r = document.querySelector('.ribbon-viewport-rect');
                return r ? r.getBoundingClientRect().width : 0;
            }"""
        )
        assert width > 0

    def test_ribbon_slider_is_focusable_keyboard_moves_valuetext(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """The slider is focusable and an arrow key updates aria-valuetext."""
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        page.wait_for_selector(".frame-ribbon .time-ribbon", timeout=30000)  # type: ignore[union-attr]
        ribbon = page.locator(".frame-ribbon")  # type: ignore[union-attr]
        assert ribbon.get_attribute("role") == "slider"
        # aria-value* are populated once the epoch bounds load.
        page.wait_for_function(  # type: ignore[union-attr]
            """() => {
                const r = document.querySelector('.frame-ribbon');
                return r && r.getAttribute('aria-valuetext');
            }""",
            timeout=10000,
        )
        ribbon.focus()  # type: ignore[union-attr]
        before = ribbon.get_attribute("aria-valuetext")
        # Step left (toward older); aria-valuetext must change.
        page.keyboard.press("ArrowLeft")  # type: ignore[union-attr]
        after = ribbon.get_attribute("aria-valuetext")
        assert before is not None and after is not None
        assert after != before

    def test_click_path_prefills_date_jump(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """The click->jump gesture also pre-fills the date-jump input.

        Dispatching the same `ribbon:jump` event the click produces is more
        robust than a pixel click and exercises the exact pre-fill code path.
        """
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        page.wait_for_selector("#frame-jump-at", timeout=30000)  # type: ignore[union-attr]
        page.wait_for_selector(".frame-ribbon .time-ribbon", timeout=30000)  # type: ignore[union-attr]
        # 2026-01-01T00:20:00Z -> the input should read the minute-trimmed value.
        ts_ms = 1767226800000  # 2026-01-01T00:20:00Z
        page.evaluate(  # type: ignore[union-attr]
            """(ms) => {
                document.querySelector('.frame-ribbon').dispatchEvent(
                    new CustomEvent('ribbon:jump', {
                        detail: { timestampMs: ms }, bubbles: true
                    })
                );
            }""",
            ts_ms,
        )
        value = page.input_value("#frame-jump-at")  # type: ignore[union-attr]
        assert value == "2026-01-01T00:20"
