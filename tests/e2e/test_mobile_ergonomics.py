"""E2E for phone ergonomics (#70 F-R10, WP-70-I; #200).

Runs in a 375x812 touch context: no page-level horizontal scroll on the main
pages, the Actions column stays on screen, touch targets are big enough, the
skip link is the first Tab stop, the scrubber ribbon owns touch drags, and the
nav marks exactly one current page.

Structural checks only (no axe-core: it is not vendored and tests run offline).
Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_PAGES = (
    "/",
    "/cameras",
    "/projects",
    "/projects/1",
    "/frames?project_id=1",
    "/renders",
    "/events",
    "/about",
    "/users",
    "/settings",
)
_MAX_SMALL_TARGETS = 10


@pytest.fixture()
def phone(
    logged_in_data_page: tuple[Any, str],
    browser_context_factory: Callable[..., Any],
) -> tuple[Any, str]:
    """A signed-in 375x812 touch page on the seeded server."""
    _, base = logged_in_data_page
    context = browser_context_factory(
        viewport={"width": 375, "height": 812}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    page.set_default_timeout(_TIMEOUT)
    return page, base


def _overflow(page: Any) -> int:
    return page.evaluate(
        "() => Math.max(document.documentElement.scrollWidth,"
        " document.body.scrollWidth) - window.innerWidth"
    )


@pytest.mark.parametrize("path", _PAGES)
def test_no_page_level_horizontal_overflow(phone: tuple[Any, str], path: str) -> None:
    page, base = phone
    page.goto(f"{base}{path}", wait_until="load")
    page.wait_for_selector(".app-main")
    assert _overflow(page) <= 0, f"{path} scrolls sideways by {_overflow(page)}px"


@pytest.mark.parametrize("path", ["/cameras", "/users"])
def test_actions_header_stays_inside_the_viewport(
    phone: tuple[Any, str], path: str
) -> None:
    page, base = phone
    page.goto(f"{base}{path}", wait_until="load")
    right = page.evaluate(
        "() => { const th = [...document.querySelectorAll('.data-table th')].pop();"
        " return th.getBoundingClientRect().right; }"
    )
    width = page.evaluate("() => window.innerWidth")
    assert right <= width + 0.5
    sticky = page.evaluate(
        "() => getComputedStyle([...document.querySelectorAll('.data-table th')]"
        ".pop()).position"
    )
    assert sticky == "sticky"


def test_touch_targets_on_the_frame_grid(phone: tuple[Any, str]) -> None:
    page, base = phone
    page.goto(f"{base}/frames?project_id=1", wait_until="load")
    page.wait_for_selector(".frame-grid .frame-tile")
    assert page.evaluate("() => matchMedia('(pointer: coarse)').matches")
    small = page.evaluate(
        """() => [...document.querySelectorAll(
              'a[href], button, select, textarea, [role=button], .chip,'
          + 'label.frame-tile-select, input:not([type=hidden]):not(.visually-hidden)')]
          .filter(el => el.offsetParent !== null)
          .map(el => {
            const r = el.getBoundingClientRect();
            return {tag: el.tagName, cls: el.className, w: r.width, h: r.height,
                    on: r.bottom > 0 && r.right > 0};
          })
          .filter(t => t.on && t.w > 1 && t.h > 1 && (t.w < 44 || t.h < 44))"""
    )
    assert len(small) <= _MAX_SMALL_TARGETS, small
    # The controls this change targets are never among the offenders.
    assert not [
        t
        for t in small
        if any(c in str(t["cls"]) for c in ("frame-tile-select", "chip"))
    ]
    assert page.evaluate(
        "() => [...document.querySelectorAll('.frame-tile-actions button')]"
        ".every(b => b.getBoundingClientRect().height >= 44)"
    )


def test_first_tab_stop_is_the_skip_link(phone: tuple[Any, str]) -> None:
    page, base = phone
    page.goto(f"{base}/cameras", wait_until="load")
    page.keyboard.press("Tab")
    assert page.evaluate("() => document.activeElement.className") == "skip-link"
    box = page.locator(".skip-link").bounding_box()
    assert box is not None and box["y"] >= 0
    page.keyboard.press("Enter")
    assert page.evaluate("() => location.hash") == "#main-content"


def test_nav_has_exactly_one_current_page(phone: tuple[Any, str]) -> None:
    page, base = phone
    for path in ("/cameras", "/projects/1", "/"):
        page.goto(f"{base}{path}", wait_until="load")
        assert page.locator(".app-nav [aria-current]").count() == 1, path
        assert page.locator(".app-nav [aria-current='page']").count() == 1, path


def test_ribbon_owns_touch_drags(phone: tuple[Any, str]) -> None:
    """#200: without touch-action the browser takes a touch drag for a scroll."""
    page, base = phone
    page.goto(f"{base}/frames?project_id=1", wait_until="load")
    page.wait_for_selector(".frame-ribbon .time-ribbon", state="attached")
    assert (
        page.locator(".frame-ribbon .time-ribbon").evaluate(
            "el => getComputedStyle(el).touchAction"
        )
        == "none"
    )


def test_pointercancel_mid_drag_leaves_no_state(phone: tuple[Any, str]) -> None:
    page, base = phone
    page.goto(f"{base}/frames?project_id=1", wait_until="load")
    ribbon = page.locator(".frame-ribbon .time-ribbon")
    ribbon.wait_for(state="attached")
    box = ribbon.bounding_box()
    assert box is not None
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] * 0.2, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.6, y, steps=8)
    page.evaluate(
        "() => document.dispatchEvent("
        "new PointerEvent('pointercancel', {bubbles: true}))"
    )
    page.mouse.up()
    page.wait_for_timeout(300)
    assert page.locator(".ribbon-drag-span").count() == 0
    assert page.locator(".scrubber-tooltip:not([hidden])").count() == 0
    assert page.locator(".frame-tile.selected").count() == 0
