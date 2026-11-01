"""E2E for the clickable dashboard project card (#23).

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses the seeded ``logged_in_data_page`` (Camera + Project id 1). With no live
supervisor the project reads as ``stopped``, so its card carries a Start form.

The Start button is never clicked. With ``capture.autostart=False`` (the e2e
and test default) it would not launch a capture loop, but it would change the
seeded project's state. Instead the tests check the card's layout: they ask
the browser which element is on top at the button's centre, which decides
whether a click acts or navigates, and that clicks elsewhere on the card open
the project, including after the dashboard's poll swaps the grid.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_CARD = "#project-card-1"

# True when the element hit-tested at the centre of ``sel`` lies inside a form,
# i.e. a click there reaches the button rather than the card overlay.
_HIT_IS_FORM_JS = """
(sel) => {
  const el = document.querySelector(sel);
  const r = el.getBoundingClientRect();
  const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
  return !!(hit && hit.closest("form"));
}
"""

# Re-run the dashboard's 30 s poll immediately and resolve once it has swapped.
# The stored poll version is dropped first: with it, an unchanged grid is
# answered 204 and not swapped at all (#70 F-P1), and this must test a swap.
_REFRESH_GRID_JS = """
() => {
  document.getElementById("projects-grid").removeAttribute("data-poll-version");
  return htmx.ajax("GET", "/partials/projects",
                   {target: "#projects-grid", swap: "innerHTML"});
}
"""


def _mouse_click_center(page: object, selector: str) -> None:
    """Click at the centre of ``selector`` as a user would.

    ``page.click(selector)`` refuses here: Playwright's actionability check sees
    the card overlay intercepting the pointer -- which is exactly the point.
    """
    box = page.locator(selector).first.bounding_box()  # type: ignore[attr-defined]
    assert box is not None
    page.mouse.click(  # type: ignore[attr-defined]
        box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    )


class TestDashboardCardClick:
    def test_click_on_stats_opens_project(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/")  # type: ignore[union-attr]
        _mouse_click_center(page, f"{_CARD} .stat-cell")
        page.wait_for_url(f"{base}/projects/1")  # type: ignore[union-attr]

    def test_click_on_thumbnail_opens_project(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/")  # type: ignore[union-attr]
        _mouse_click_center(page, f"{_CARD} .project-card-thumb")
        page.wait_for_url(f"{base}/projects/1")  # type: ignore[union-attr]

    def test_action_button_sits_above_overlay(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/")  # type: ignore[union-attr]
        button = f"{_CARD} .project-card-footer button"
        assert page.evaluate(_HIT_IS_FORM_JS, button)  # type: ignore[union-attr]
        # A stat cell, by contrast, is covered by the overlay link.
        assert page.evaluate(  # type: ignore[union-attr]
            """(sel) => {
              const r = document.querySelector(sel).getBoundingClientRect();
              const hit = document.elementFromPoint(r.left + 4, r.top + 4);
              return !!(hit && hit.classList.contains("project-card-link"));
            }""",
            f"{_CARD} .stat-cell",
        )

    def test_behaviour_survives_poll_swap(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/")  # type: ignore[union-attr]
        page.evaluate(_REFRESH_GRID_JS)  # type: ignore[union-attr]
        page.wait_for_selector(_CARD)  # type: ignore[union-attr]
        button = f"{_CARD} .project-card-footer button"
        assert page.evaluate(_HIT_IS_FORM_JS, button)  # type: ignore[union-attr]
        _mouse_click_center(page, f"{_CARD} .project-card-stats")
        page.wait_for_url(f"{base}/projects/1")  # type: ignore[union-attr]

    def test_enter_on_focused_card_opens_project(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/")  # type: ignore[union-attr]
        page.focus(f"{_CARD} .project-card-link")  # type: ignore[union-attr]
        page.keyboard.press("Enter")  # type: ignore[union-attr]
        page.wait_for_url(f"{base}/projects/1")  # type: ignore[union-attr]
