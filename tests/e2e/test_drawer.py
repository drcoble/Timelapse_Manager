"""E2E tests for the right-side drawer system.

Covers: open/close lifecycle, keyboard/backdrop dismiss, accessibility
attributes, focus management, and the no-JS fallback path.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (seeds a Camera with protocol="vapix" so
sole-camera auto-select fires and the new-project form renders).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Timeout in ms for async operations (HTMX swaps, CSS transitions, focus).
_TIMEOUT = 30_000

# Predicates polled with wait_for_function — kept as module constants so the
# long selector strings live in one place (and stay under the line limit).
_DRAWER_OPEN_JS = (
    "() => document.querySelector('#drawer-main')"
    ".getAttribute('aria-hidden') === 'false'"
)
_DRAWER_CLOSED_JS = (
    "() => document.querySelector('#drawer-main')"
    ".getAttribute('aria-hidden') === 'true'"
)


class TestNewProjectDrawerLifecycle:
    """Opening and closing the New Project drawer from the dashboard."""

    def test_clicking_new_project_opens_drawer_with_correct_state(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """Clicking '+ New Project' loads the fragment, opens the drawer, and
        puts the page into the scroll-locked modal state."""
        page, base = logged_in_data_page  # type: ignore[misc]

        page.goto(f"{base}/", wait_until="domcontentloaded")  # type: ignore[union-attr]

        # Click the first opener link (dashboard page-header button).
        opener = page.locator('a[hx-get="/drawers/new-project"]').first  # type: ignore[union-attr]
        opener.click()

        # Drawer must become visible.
        page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]

        # Drawer title set by JS from data-drawer-title attribute.
        title_el = page.locator("#drawer-title")  # type: ignore[union-attr]
        assert title_el.inner_text(timeout=_TIMEOUT) == "New Project"

        # Body carries scroll-lock class.
        assert page.evaluate("document.body.classList.contains('drawer-open')")  # type: ignore[union-attr]

        # Regions outside the drawer are inert.
        assert page.evaluate(  # type: ignore[union-attr]
            "document.querySelector('.app-nav').hasAttribute('inert')"
        )
        assert page.evaluate(  # type: ignore[union-attr]
            "document.querySelector('.app-main').hasAttribute('inert')"
        )

    def test_escape_closes_drawer_and_returns_focus_to_opener(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """Pressing Escape dismisses the drawer and restores focus to the opener."""
        page, base = logged_in_data_page  # type: ignore[misc]

        page.goto(f"{base}/", wait_until="domcontentloaded")  # type: ignore[union-attr]

        opener = page.locator('a[hx-get="/drawers/new-project"]').first  # type: ignore[union-attr]
        opener.click()

        # Wait for the drawer to open, then let the keydown listener attach.
        page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]
        page.wait_for_timeout(200)  # type: ignore[union-attr]

        # Dismiss with Escape.
        page.keyboard.press("Escape")  # type: ignore[union-attr]

        # Drawer must be hidden again.
        page.wait_for_function(_DRAWER_CLOSED_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]

        # Scroll-lock removed.
        assert not page.evaluate("document.body.classList.contains('drawer-open')")  # type: ignore[union-attr]

        # Focus returns to the element that opened the drawer.
        try:
            page.wait_for_function(  # type: ignore[union-attr]
                "() => { var el = document.activeElement; return el "
                "&& el.getAttribute "
                "&& el.getAttribute('hx-get') === '/drawers/new-project'; }",
                timeout=3000,
            )
            active_hx_get = "/drawers/new-project"
        except Exception:  # noqa: BLE001
            active_hx_get = page.evaluate(  # type: ignore[union-attr]
                "document.activeElement "
                "&& document.activeElement.getAttribute('hx-get')"
            )
        assert active_hx_get == "/drawers/new-project"

    def test_backdrop_click_closes_drawer(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """Clicking the backdrop overlay dismisses the drawer."""
        page, base = logged_in_data_page  # type: ignore[misc]

        page.goto(f"{base}/", wait_until="domcontentloaded")  # type: ignore[union-attr]

        opener = page.locator('a[hx-get="/drawers/new-project"]').first  # type: ignore[union-attr]
        opener.click()

        page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]

        # The backdrop may be partially behind the drawer panel; force=True is
        # correct here because drawer.js checks e.target, not actionability.
        page.locator(".drawer-backdrop").click(force=True)  # type: ignore[union-attr]

        page.wait_for_function(_DRAWER_CLOSED_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]
        assert not page.evaluate("document.body.classList.contains('drawer-open')")  # type: ignore[union-attr]

    def test_drawer_focus_lands_on_name_field(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """After the drawer opens, focus lands on the first visible field — the
        project name input — not the hidden CSRF input that precedes it in DOM
        order (drawer.js skips non-visible focusables)."""
        page, base = logged_in_data_page  # type: ignore[misc]

        page.goto(f"{base}/", wait_until="domcontentloaded")  # type: ignore[union-attr]

        opener = page.locator('a[hx-get="/drawers/new-project"]').first  # type: ignore[union-attr]
        opener.click()

        page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]
        page.wait_for_function(  # type: ignore[union-attr]
            "() => document.activeElement && document.activeElement.id === 'name'",
            timeout=_TIMEOUT,
        )
        assert page.evaluate("document.activeElement.id") == "name"  # type: ignore[union-attr]


class TestNoJsFallback:
    """Navigating directly to a drawer route without HTMX returns a full page."""

    def test_direct_navigation_to_new_project_route_renders_full_page(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """GET /drawers/new-project without HX-Request returns the full page
        (new_project.html extending base.html), which includes the app nav."""
        page, base = logged_in_data_page  # type: ignore[misc]

        # Navigate directly — no HX-Request header, so the route returns the
        # full page template (new_project.html extends base.html).
        page.goto(f"{base}/drawers/new-project", wait_until="domcontentloaded")  # type: ignore[union-attr]

        # Full page includes the app nav shell.
        page.wait_for_selector(".app-nav", timeout=_TIMEOUT)  # type: ignore[union-attr]
        assert page.locator(".app-nav").count() >= 1  # type: ignore[union-attr]

        # Form is still present in the full-page render.
        assert page.locator("#name").count() >= 1  # type: ignore[union-attr]


class TestNewUserDrawer:
    """Opening and closing the New User drawer from the /users page."""

    def test_add_user_button_opens_new_user_drawer(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """Clicking 'Add User' on /users loads the new-user fragment and opens
        the drawer with the title 'New User'."""
        page, base = logged_in_data_page  # type: ignore[misc]

        page.goto(f"{base}/users", wait_until="domcontentloaded")  # type: ignore[union-attr]

        # Locate the drawer opener on the users page.
        opener = page.locator('a[hx-get="/drawers/new-user"]').first  # type: ignore[union-attr]
        opener.click()

        # Drawer must open.
        page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]

        # Title must reflect the new-user drawer.
        title_el = page.locator("#drawer-title")  # type: ignore[union-attr]
        assert title_el.inner_text(timeout=_TIMEOUT) == "New User"

    def test_cancel_button_closes_new_user_drawer(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """Clicking the 'Cancel' button ([data-drawer-close]) in the new-user
        form closes the drawer.  Using role/name to disambiguate from the
        header × close button, which also carries [data-drawer-close]."""
        page, base = logged_in_data_page  # type: ignore[misc]

        page.goto(f"{base}/users", wait_until="domcontentloaded")  # type: ignore[union-attr]

        opener = page.locator('a[hx-get="/drawers/new-user"]').first  # type: ignore[union-attr]
        opener.click()

        page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]

        # Click the form Cancel button (not the header × which is labelled "Close").
        cancel_btn = page.get_by_role("button", name="Cancel")  # type: ignore[union-attr]
        cancel_btn.click()

        # Drawer must be dismissed.
        page.wait_for_function(_DRAWER_CLOSED_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]
        assert not page.evaluate("document.body.classList.contains('drawer-open')")  # type: ignore[union-attr]
