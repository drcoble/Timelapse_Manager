"""E2E for the frame grid's weight controls (#70 F-P4, WP-70-K).

* A long scroll stops at 600 tiles and ends in a cap whose link continues from
  the oldest loaded frame.
* Selecting a range at 600 tiles stays fast (loose bound; the number is logged).
* The per-tile Remove control is an htmx button now, not a form, and still works.
* A scrubber drag the browser cancels (touch scroll) strands nothing.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from collections.abc import Generator
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.conftest import _boot_app, login, make_frame_seeder, stop_app

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_TOTAL_FRAMES = 1600
_TILE_CAP = 600
# Selecting every tile in range at the cap; generous so a loaded CI box passes.
_SET_DESCRIPTOR_BUDGET_MS = 150


@pytest.fixture()
def long_series(
    tmp_path: Path, _chromium_page: Any
) -> Generator[tuple[Any, str], None, None]:
    """A signed-in admin page on a server with project 1 and 1600 frames."""
    proc, base_url = _boot_app(
        tmp_path, extra_seed=make_frame_seeder(_TOTAL_FRAMES, project_name="Long")
    )
    try:
        login(_chromium_page, base_url)
        yield _chromium_page, base_url
    finally:
        stop_app(proc)


def _scroll_to_cap(page: Any) -> None:
    """Scroll each fresh sentinel into view until the cap replaces it."""
    page.wait_for_selector(".frame-grid .frame-tile", timeout=_TIMEOUT)
    for expected in range(120, _TILE_CAP + 1, 60):
        page.locator(".frame-sentinel").scroll_into_view_if_needed()
        page.wait_for_function(
            "(n) => document.querySelectorAll('#frame-grid .frame-tile').length >= n",
            arg=expected,
            timeout=_TIMEOUT,
        )


def test_scroll_stops_at_the_cap_and_link_continues(
    long_series: tuple[Any, str],
) -> None:
    page, base = long_series
    page.goto(f"{base}/frames?project_id=1", wait_until="domcontentloaded")
    _scroll_to_cap(page)

    cap = page.locator(".frame-end-cap")
    cap.wait_for(state="attached", timeout=_TIMEOUT)
    assert page.locator("#frame-grid .frame-tile").count() == _TILE_CAP
    assert page.locator(".frame-sentinel").count() == 0
    assert "Jump further back" in cap.inner_text()

    oldest_loaded = page.evaluate(
        "() => { const t = document.querySelectorAll('#frame-grid .frame-tile');"
        " return t[t.length - 1].id; }"
    )
    cap.locator("a.frame-end-cap-jump").click()
    page.wait_for_url("**/frames?project_id=1&at=*", timeout=_TIMEOUT)
    page.wait_for_selector(f"#{oldest_loaded}", timeout=_TIMEOUT)
    # The new window is centred on that frame, so older frames are present too.
    assert page.locator("#frame-grid .frame-tile").count() > 1


def test_select_range_at_the_cap_is_fast(long_series: tuple[Any, str]) -> None:
    page, base = long_series
    page.goto(f"{base}/frames?project_id=1", wait_until="domcontentloaded")
    _scroll_to_cap(page)
    page.wait_for_function(
        "() => window.frameSelection && window.frameSelection.setDescriptor",
        timeout=_TIMEOUT,
    )
    elapsed_ms = page.evaluate(
        """() => {
          const t0 = performance.now();
          window.frameSelection.setDescriptor(
            {
              scope: "in_range",
              project_id: 1,
              time_range: { from: "2026-01-01T00:00:00Z", to: "2026-12-31T23:59:59Z" },
              filters: { include_deleted: false },
              deselected_ids: [],
            },
            1600,
            "all"
          );
          return performance.now() - t0;
        }"""
    )
    print(f"setDescriptor over {_TILE_CAP} tiles: {elapsed_ms:.1f} ms")
    assert elapsed_ms < _SET_DESCRIPTOR_BUDGET_MS
    selected = page.locator("#frame-grid .frame-tile[data-selected='true']").count()
    assert selected == _TILE_CAP


def test_remove_button_soft_deletes_the_tile(long_series: tuple[Any, str]) -> None:
    page, base = long_series
    page.goto(f"{base}/frames?project_id=1", wait_until="domcontentloaded")
    tile = page.locator("#frame-grid .frame-tile").first
    tile.wait_for(state="attached", timeout=_TIMEOUT)
    tile_id = tile.get_attribute("id")
    # A button, not a per-tile form.
    assert page.locator(f"#{tile_id} form").count() == 0
    button = page.locator(f"#{tile_id} .frame-tile-actions button")
    assert button.get_attribute("hx-post", timeout=_TIMEOUT)
    with page.expect_response(lambda r: "/soft-delete" in r.url) as resp:
        button.click()
    assert resp.value.status == 200
    page.wait_for_selector(f"#{tile_id}.soft-deleted", timeout=_TIMEOUT)
    # The re-rendered tile offers Restore, again as a button.
    restore = page.locator(f"#{tile_id} .frame-tile-actions button")
    assert "Restore" in restore.inner_text()


def test_cancelled_scrubber_drag_selects_nothing(
    long_series: tuple[Any, str],
) -> None:
    page, base = long_series
    page.goto(f"{base}/frames?project_id=1", wait_until="domcontentloaded")
    ribbon = page.locator(".frame-ribbon .time-ribbon")
    ribbon.wait_for(state="attached", timeout=_TIMEOUT)
    box = ribbon.bounding_box()
    assert box is not None
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + box["width"] * 0.2, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] * 0.6, y, steps=8)
    # The browser takes the gesture over (a touch scroll): pointercancel, no up.
    page.evaluate(
        "() => document.dispatchEvent("
        "new PointerEvent('pointercancel', {bubbles: true}))"
    )
    page.mouse.up()
    page.wait_for_timeout(300)
    assert page.locator(".ribbon-drag-span").count() == 0
    assert page.locator("#frames-escalation-banner").is_hidden()
