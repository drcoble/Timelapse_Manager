"""E2E for the closed drawer's accessibility and the header menu (#70 F-R7, WP-70-I).

* A closed drawer is `inert`, `visibility:hidden` and empty, so keyboard focus can
  never reach it and typed credentials are not left in a hidden DOM.
* A drawer fetch that fails opens nothing, shows a toast, forgets the pending
  open, and the next drawer opens normally.
* #205: the header user menu opens on focus or click, not hover, so after the
  drawer's close button is clicked its dropdown never sits over "+ New Project".

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_OPENER = 'a[hx-get="/drawers/new-project"]'
_DRAWER_OPEN_JS = (
    "() => document.querySelector('#drawer-main')"
    ".getAttribute('aria-hidden') === 'false'"
)
_DRAWER_CLOSED_JS = (
    "() => { const d = document.querySelector('#drawer-main');"
    " return d.getAttribute('aria-hidden') === 'true'"
    " && getComputedStyle(d).visibility === 'hidden'; }"
)


def _open_new_project(page: Any, base: str) -> None:
    page.goto(f"{base}/", wait_until="domcontentloaded")
    page.locator(_OPENER).first.click()
    page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)
    page.wait_for_function(
        "() => document.activeElement && document.activeElement.id === 'name'",
        timeout=_TIMEOUT,
    )


def test_closed_drawer_is_inert_hidden_and_empty(
    logged_in_data_page: tuple[Any, str],
) -> None:
    page, base = logged_in_data_page
    drawer = page.locator("#drawer-main")
    page.goto(f"{base}/", wait_until="domcontentloaded")
    # Closed from the first byte: inert and hidden before any script runs.
    assert drawer.evaluate("d => d.hasAttribute('inert')")
    assert drawer.evaluate("d => getComputedStyle(d).visibility") == "hidden"

    _open_new_project(page, base)
    assert not drawer.evaluate("d => d.hasAttribute('inert')")
    assert drawer.evaluate("d => getComputedStyle(d).visibility") == "visible"
    assert page.locator("#drawer-main .drawer-body *").count() > 0

    page.wait_for_timeout(200)  # let the keydown listener attach
    page.keyboard.press("Escape")
    page.wait_for_function(_DRAWER_CLOSED_JS, timeout=_TIMEOUT)

    assert drawer.evaluate("d => d.hasAttribute('inert')")
    assert drawer.evaluate("d => getComputedStyle(d).visibility") == "hidden"
    # The body is emptied once the slide-out ends, which clears typed fields too.
    page.wait_for_function(
        "() => document.querySelector('#drawer-main .drawer-body')"
        ".childElementCount === 0",
        timeout=_TIMEOUT,
    )
    assert page.locator("#drawer-main .drawer-body").inner_text().strip() == ""


def test_tab_never_reaches_a_closed_drawer(
    logged_in_data_page: tuple[Any, str],
) -> None:
    page, base = logged_in_data_page
    _open_new_project(page, base)
    page.wait_for_timeout(200)
    page.keyboard.press("Escape")
    page.wait_for_function(_DRAWER_CLOSED_JS, timeout=_TIMEOUT)

    for _ in range(40):
        page.keyboard.press("Tab")
        assert not page.evaluate(
            "() => !!document.activeElement"
            " && !!document.activeElement.closest('#drawer-main')"
        )


def test_failed_drawer_fetch_opens_nothing_and_next_open_works(
    logged_in_data_page: tuple[Any, str],
) -> None:
    page, base = logged_in_data_page
    page.goto(f"{base}/", wait_until="domcontentloaded")

    page.route("**/drawers/new-project", lambda r: r.fulfill(status=500, body="boom"))
    page.locator(_OPENER).first.click()
    page.wait_for_selector("#toast-region .toast", timeout=_TIMEOUT)
    assert page.locator("#drawer-main").get_attribute("aria-hidden") == "true"
    page.unroute("**/drawers/new-project")

    # The failed open is forgotten: an unrelated load into the drawer body must
    # not be mistaken for the pending open and pop the drawer.
    page.evaluate(
        "() => htmx.ajax('GET', '/drawers/new-project',"
        " {target: '#drawer-main .drawer-body', swap: 'innerHTML'})"
    )
    page.wait_for_selector("#drawer-main .drawer-body *", state="attached")
    page.wait_for_timeout(300)
    assert page.locator("#drawer-main").get_attribute("aria-hidden") == "true"

    page.locator(_OPENER).first.click()
    page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)
    assert page.locator("#drawer-title").inner_text() == "New Project"


def test_new_project_opens_right_after_closing_with_the_close_button(
    logged_in_data_page: tuple[Any, str],
) -> None:
    """#205: the pointer rests where the close button was; the user menu must not
    open under it and swallow the next click on the "+ New Project" opener."""
    page, base = logged_in_data_page
    _open_new_project(page, base)
    page.wait_for_timeout(200)

    page.locator("#drawer-main [data-drawer-close]").first.click()
    page.wait_for_function(_DRAWER_CLOSED_JS, timeout=_TIMEOUT)

    box = page.locator(_OPENER).first.bounding_box()
    assert box is not None
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    # Nothing but the opener is hit at its centre, and the menu stays shut.
    assert page.evaluate(
        "([x, y]) => !!document.elementFromPoint(x, y)"
        ".closest('a[hx-get=\"/drawers/new-project\"]')",
        [x, y],
    )
    assert (
        page.locator(".user-dropdown").evaluate("d => getComputedStyle(d).display")
        == "none"
    )

    page.mouse.click(x, y)
    page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)
    assert page.locator("#drawer-title").inner_text() == "New Project"


def test_user_menu_opens_on_click_and_keyboard(
    logged_in_data_page: tuple[Any, str],
) -> None:
    page, base = logged_in_data_page
    page.goto(f"{base}/", wait_until="domcontentloaded")
    dropdown = page.locator(".user-dropdown")
    assert dropdown.evaluate("d => getComputedStyle(d).display") == "none"

    page.locator(".user-menu").click()
    assert dropdown.evaluate("d => getComputedStyle(d).display") == "block"
    page.locator("main").click(position={"x": 5, "y": 5}, force=True)
    assert dropdown.evaluate("d => getComputedStyle(d).display") == "none"

    page.locator(".user-menu").focus()
    assert dropdown.evaluate("d => getComputedStyle(d).display") == "block"
    assert page.locator(".user-dropdown a[role=menuitem]").is_visible()
