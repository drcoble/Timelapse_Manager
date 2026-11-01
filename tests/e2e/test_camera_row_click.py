"""The camera list: a click anywhere on a row opens the camera (#302).

A row click does what Row actions -> Edit does (the inline edit form). Controls in
the row keep their own behaviour, a drag-select of the address does not open the
camera, the name is a real focusable button, and a row that htmx swapped in (an
edit save) is clickable without a reload. The Add Camera flow redirects to a full
page load, so its new row is plain server markup; the swapped row is the case
that needs the delegated listener.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.e2e._row_click_support import arm_click_probe, settle

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_ROW = "#camera-row-1"
_FORM = f"{_ROW} form[hx-post='/cameras/1/edit']"

# True once htmx has processed the name button; a click before that would do nothing.
_NAME_BOUND_JS = (
    "() => !!document.querySelector('#camera-row-1 .camera-open-link')"
    "?.['htmx-internal-data']"
)


def _open_list(page: Any, base: str) -> None:
    page.goto(f"{base}/cameras", wait_until="domcontentloaded")
    page.locator(_ROW).wait_for(timeout=_TIMEOUT)
    page.wait_for_function(_NAME_BOUND_JS, timeout=_TIMEOUT)
    page.wait_for_load_state("load", timeout=_TIMEOUT)
    page.evaluate("() => document.fonts.ready.then(() => true)")
    arm_click_probe(page)


def test_row_is_marked_clickable(logged_in_data_page: tuple[Any, str]) -> None:
    page, base = logged_in_data_page
    _open_list(page, base)
    cursor = page.eval_on_selector(_ROW, "el => getComputedStyle(el).cursor")
    assert cursor == "pointer"


@pytest.mark.parametrize("cell", [3, 4])
def test_clicking_a_cell_opens_the_edit_form(
    logged_in_data_page: tuple[Any, str], cell: int
) -> None:
    """Cell 3 is the address, 4 the geolocation."""
    page, base = logged_in_data_page
    _open_list(page, base)
    page.locator(f"{_ROW} td:nth-child({cell})").click()
    page.locator(_FORM).wait_for(timeout=_TIMEOUT)


def test_actions_trigger_opens_only_the_menu(
    logged_in_data_page: tuple[Any, str],
) -> None:
    page, base = logged_in_data_page
    _open_list(page, base)
    page.locator(f"{_ROW} .row-actions-trigger").click()
    page.locator(f"{_ROW} .row-actions-popover").wait_for(
        state="visible", timeout=_TIMEOUT
    )
    # Wait for the click to be handled and the network idle before asserting.
    settle(page)
    assert page.locator(_FORM).count() == 0


def test_delete_asks_for_confirmation_and_does_not_open(
    logged_in_data_page: tuple[Any, str],
) -> None:
    page, base = logged_in_data_page
    _open_list(page, base)
    messages: list[str] = []

    def _dismiss(dialog: Any) -> None:
        messages.append(dialog.message)
        dialog.dismiss()

    page.on("dialog", _dismiss)
    page.locator(f"{_ROW} .row-actions-trigger").click()
    page.locator(f"{_ROW} .row-actions-popover button.danger").click()
    settle(page)
    assert len(messages) == 1 and "Delete camera" in messages[0]
    assert page.locator(_FORM).count() == 0
    assert page.locator(_ROW).count() == 1


def test_tab_to_the_name_and_press_enter(
    logged_in_data_page: tuple[Any, str],
) -> None:
    page, base = logged_in_data_page
    _open_list(page, base)
    page.focus(f"{_ROW} .camera-open-link")
    page.keyboard.press("Enter")
    page.locator(_FORM).wait_for(timeout=_TIMEOUT)


def test_drag_select_of_the_address_does_not_open(
    logged_in_data_page: tuple[Any, str],
) -> None:
    page, base = logged_in_data_page
    _open_list(page, base)
    box = page.locator(f"{_ROW} td:nth-child(3)").bounding_box()
    y = box["y"] + box["height"] / 2
    page.mouse.move(box["x"] + 2, y)
    page.mouse.down()
    page.mouse.move(box["x"] + box["width"] - 2, y, steps=8)
    page.mouse.up()
    assert page.evaluate("() => String(window.getSelection()).length") > 0
    settle(page)
    assert page.locator(_FORM).count() == 0


def test_a_row_swapped_in_by_htmx_is_clickable(
    logged_in_data_page: tuple[Any, str],
) -> None:
    """Saving the edit form swaps the row back in; the new row opens on a click."""
    page, base = logged_in_data_page
    _open_list(page, base)
    page.locator(f"{_ROW} td:nth-child(3)").click()
    page.locator(_FORM).wait_for(timeout=_TIMEOUT)
    page.wait_for_function(
        "() => !!document.querySelector('#camera-row-1 form')?.['htmx-internal-data']",
        timeout=_TIMEOUT,
    )
    page.locator(f"{_FORM} button[type='submit']").first.click()
    page.locator(f"{_ROW}.camera-row-clickable").wait_for(timeout=_TIMEOUT)
    assert page.locator(_FORM).count() == 0
    page.locator(f"{_ROW} td:nth-child(3)").click()
    page.locator(_FORM).wait_for(timeout=_TIMEOUT)
