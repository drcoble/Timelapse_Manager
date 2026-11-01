"""A closed centred modal is out of the tab order; an open one takes focus (#277).

``.modal-backdrop`` used to hide a closed modal with ``opacity: 0`` alone, so Tab
walked onto its invisible controls. It is now ``visibility: hidden`` (flipped
after the fade-out). Requires the ``ui`` marker; auto-skips when Playwright
browsers are absent.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_IN_MODAL_JS = "() => !!document.activeElement?.closest('.discover-modal')"
# Enough presses to walk every tab stop on /cameras more than once.
_TAB_PRESSES = 60


class TestClosedModalTabOrder:
    def test_tab_never_focuses_inside_a_closed_modal(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        page.wait_for_selector("[data-modal-open='#discover-modal']", timeout=30000)  # type: ignore[union-attr]
        assert page.locator("#discover-modal").get_attribute("aria-hidden") == "true"  # type: ignore[union-attr]
        for _ in range(_TAB_PRESSES):
            page.keyboard.press("Tab")  # type: ignore[union-attr]
            assert not page.evaluate(_IN_MODAL_JS), "Tab reached a closed modal"  # type: ignore[union-attr]

    def test_opening_the_modal_moves_focus_into_it(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/cameras")  # type: ignore[union-attr]
        page.click("[data-modal-open='#discover-modal']")  # type: ignore[union-attr]
        page.wait_for_function(_IN_MODAL_JS, timeout=10000)  # type: ignore[union-attr]
        # Closed again, the modal is unreachable once the fade-out has ended.
        page.keyboard.press("Escape")  # type: ignore[union-attr]
        page.wait_for_function(  # type: ignore[union-attr]
            "() => getComputedStyle(document.querySelector('.modal-backdrop'))"
            ".visibility === 'hidden'",
            timeout=10000,
        )
