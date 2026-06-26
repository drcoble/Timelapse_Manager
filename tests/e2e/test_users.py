"""E2E for the Users admin screen: the CSS row-actions popover.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` which seeds a second (non-self) user ``viewer1``
so exactly one row carries the actions menu (the admin's own row shows "(you)").
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestUsersRowActions:
    def test_popover_hidden_until_hover_then_reveals_actions(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/users")  # type: ignore[union-attr]
        page.wait_for_selector(".row-actions-menu", timeout=30000)  # type: ignore[union-attr]
        popover = page.locator(".row-actions-popover").first  # type: ignore[union-attr]
        # CSS-driven: hidden at rest, revealed on hover of the menu.
        assert not popover.is_visible()
        page.locator(".row-actions-trigger").first.hover()  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            ".row-actions-popover", state="visible", timeout=5000
        )
        assert popover.is_visible()
        assert popover.get_by_role("menuitem", name="Edit role").is_visible()
        assert popover.get_by_role("menuitem", name="Delete").is_visible()

    def test_self_row_shows_you_marker(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/users")  # type: ignore[union-attr]
        page.wait_for_selector("#users-tbody", timeout=30000)  # type: ignore[union-attr]
        assert page.get_by_text("(you)").count() == 1  # type: ignore[union-attr]
