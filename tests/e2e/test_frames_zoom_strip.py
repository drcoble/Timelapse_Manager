"""E2E for the scrubber zoom strip — the finer ribbon of the loaded window.

The default data fixture's frames sit at 2026-01-01 and the project has no end
date, so the campaign spans start->now (well over the 60-day zoom threshold). The
scrubber therefore magnifies the loaded window into the sibling .frame-zoom-strip.
The strip is interactive: a click maps x->timestamp and drives a jump via the
same ribbon:jump path, which scrubber.js mirrors into the date-jump input.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (project id 1, single-project, so the controls show).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000


class TestZoomStrip:
    def test_long_campaign_shows_finer_zoom_strip(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        # Overview ribbon loads first; it carries the full-campaign epoch bounds.
        overview = page.locator(".frame-ribbon .time-ribbon")  # type: ignore[union-attr]
        overview.wait_for(timeout=_TIMEOUT)
        # The zoom strip is lazy-loaded once the viewport reflects the window.
        zoom = page.locator(".frame-zoom-strip .time-ribbon")  # type: ignore[union-attr]
        zoom.wait_for(timeout=_TIMEOUT)
        # It is interactive (a click drives a jump), not a dead decorative bar.
        assert (
            page.locator(  # type: ignore[union-attr]
                ".frame-zoom-strip .time-ribbon-svg--interactive"
            ).count()
            == 1
        )
        # The zoom window is a strict sub-span of the campaign (the magnification).
        camp_start = int(overview.get_attribute("data-start"))  # type: ignore[union-attr]
        camp_end = int(overview.get_attribute("data-end"))  # type: ignore[union-attr]
        z_start = int(zoom.get_attribute("data-start"))  # type: ignore[union-attr]
        z_end = int(zoom.get_attribute("data-end"))  # type: ignore[union-attr]
        assert (z_end - z_start) < (camp_end - camp_start)
        assert z_start >= camp_start and z_end <= camp_end

    def test_zoom_strip_click_drives_a_jump(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        page.locator(".frame-zoom-strip .time-ribbon").wait_for(  # type: ignore[union-attr]
            timeout=_TIMEOUT
        )
        # Click the zoom strip: ribbon.js maps the x-position to a timestamp and
        # dispatches ribbon:jump, which scrubber.js mirrors into the date-jump
        # input. Asserting the pre-fill proves the click really drove a jump.
        page.locator(".frame-zoom-strip .time-ribbon-svg").click(  # type: ignore[union-attr]
            position={"x": 40, "y": 10}
        )
        page.wait_for_function(  # type: ignore[union-attr]
            "() => { const i = document.getElementById('frame-jump-at');"
            " return i && i.value ? true : false; }",
            timeout=_TIMEOUT,
        )
        assert page.locator("#frame-jump-at").input_value()  # type: ignore[union-attr]
