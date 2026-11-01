"""The row-actions popover is not clipped by the table's card wrapper (#210).

The cameras table sits in a card wrapper with ``overflow-x: auto``, which clips
an absolutely positioned popover hanging below the last row. ``row-actions.js``
places the open popover with ``position: fixed`` instead. The seeded data has one
camera, so its row is the last (and only) one: the case where the popover has the
least wrapper to hang in.

A clipped item cannot be hit, so the check asks the browser which element is at
the centre of each menu item.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_CAMERA_ID = 1

_HIT_TEST = """
(menuSelector) => {
  const menu = document.querySelector(menuSelector);
  const items = Array.from(
    menu.querySelectorAll('.row-actions-popover [role=menuitem]'));
  const root = document.documentElement;
  const view = {w: root.clientWidth, h: root.clientHeight};
  return items.map((item) => {
    const r = item.getBoundingClientRect();
    const x = r.left + r.width / 2;
    const y = r.top + r.height / 2;
    const hit = document.elementFromPoint(x, y);
    return {
      label: item.textContent.trim(),
      inView: r.left >= 0 && r.top >= 0 && r.right <= view.w && r.bottom <= view.h,
      hit: !!hit && item.contains(hit),
    };
  });
}
"""


def _open_menu(page: Any, base: str) -> str:
    page.goto(f"{base}/cameras", wait_until="domcontentloaded")
    row = page.locator(f"#camera-row-{_CAMERA_ID}")
    row.wait_for(timeout=_TIMEOUT)
    assert page.locator("table.data-table tbody tr").count() == 1
    row.locator(".row-actions-trigger").click()
    page.locator(f"#camera-row-{_CAMERA_ID} .row-actions-popover").wait_for(
        state="visible", timeout=_TIMEOUT
    )
    return f"#camera-row-{_CAMERA_ID} .row-actions-menu"


def test_last_row_popover_items_are_reachable(
    logged_in_data_page: tuple[Any, str],
) -> None:
    page, base = logged_in_data_page
    menu = _open_menu(page, base)
    results = page.evaluate(_HIT_TEST, menu)
    assert [r["label"] for r in results] == ["Edit", "Test connection", "Delete"]
    for item in results:
        assert item["hit"], f"{item['label']} is clipped"
        assert item["inView"], f"{item['label']} is outside the viewport"


def test_popover_opens_upward_when_there_is_no_room_below(
    logged_in_data_page: tuple[Any, str],
) -> None:
    """A short viewport leaves no room under the row, so the menu flips up."""
    page, base = logged_in_data_page
    page.set_viewport_size({"width": 1100, "height": 330})
    menu = _open_menu(page, base)
    trigger = page.locator(f"{menu} .row-actions-trigger").bounding_box()
    popover = page.locator(f"{menu} .row-actions-popover").bounding_box()
    assert popover["y"] + popover["height"] <= trigger["y"] + 1
    for item in page.evaluate(_HIT_TEST, menu):
        assert item["hit"], f"{item['label']} is clipped"
