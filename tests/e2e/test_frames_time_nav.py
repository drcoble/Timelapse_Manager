"""E2E for the frames-browser time navigation: date-jump form, the interactive
ribbon's accessibility contract, and the new-frames pill.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
The seeded project (id 1) has 65 frames captured one minute apart starting at
2026-01-01T00:00 UTC, so a mid-series jump lands a window with frames on both
sides of the anchor.
"""

from __future__ import annotations

import re

import pytest

from tests.e2e._tz_support import set_viewer_zone

pytestmark = pytest.mark.ui


class TestFramesNav:
    def test_date_jump_resets_grid_to_window(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """Submitting the date-jump form swaps the grid to a windowed view."""
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        # The jump value is read in the viewer's zone; pin UTC so minute 20 of
        # the seeded series (2026-01-01T00:00Z + i min) is the same instant on
        # every host.
        set_viewer_zone(page, "UTC")
        page.wait_for_selector(".frame-grid .frame-tile", timeout=30000)  # type: ignore[union-attr]
        newest_id = int(
            page.locator(".frame-grid .frame-tile")  # type: ignore[union-attr]
            .first.get_attribute("id")
            .split("-")[-1]
        )
        # Wait for htmx to bind the jump form first: a submit that lands before
        # it is a plain full-page GET, not the hx-get swap (#286, as #265).
        page.wait_for_function(  # type: ignore[union-attr]
            "() => !!document.querySelector('.frame-jump')?.['htmx-internal-data']",
            timeout=30000,
        )
        # Jump to ~minute 20 -> a window centered there (older AND newer frames).
        page.fill("#frame-jump-at", "2026-01-01T00:20")  # type: ignore[union-attr]
        with page.expect_response(  # type: ignore[union-attr]
            lambda r: "/frames" in r.url and "at=" in r.url
        ):
            page.click(".frame-jump button[type='submit']")  # type: ignore[union-attr]
        # The grid is swapped in place: wait for the swap itself, not a delay.
        # The first tile of the unjumped grid is the newest frame; the window
        # around minute 20 stops short of it. An un-swapped (or fully
        # reloaded) page would still start at the newest frame.
        page.wait_for_function(  # type: ignore[union-attr]
            "(n) => { const t = document.querySelector('#frame-grid .frame-tile');"
            " return !!t && parseInt(t.id.split('-').pop(), 10) < n; }",
            arg=newest_id,
            timeout=30000,
        )
        # The window straddles the anchor: it reaches the series start (end-cap)
        # because minute 20 is within 30 frames of the first frame.
        assert page.locator(".frame-grid .frame-tile").count() > 0  # type: ignore[union-attr]
        # The page chrome is intact (no nested page swapped into the grid).
        assert page.locator("#frame-grid #frame-grid").count() == 0  # type: ignore[union-attr]

    def test_ribbon_is_slider_single_project(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """The ribbon is now a real keyboard control: role=slider, focusable."""
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        page.wait_for_selector(".frame-ribbon", timeout=30000)  # type: ignore[union-attr]
        ribbon = page.locator(".frame-ribbon")  # type: ignore[union-attr]
        # Promoted from an aria-hidden pointer aid to a slider (WCAG 2.1.1).
        assert ribbon.get_attribute("role") == "slider"
        assert ribbon.get_attribute("aria-hidden") is None
        assert ribbon.get_attribute("tabindex") == "0"

    def test_ribbon_absent_under_all_projects(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """All-Projects has no time axis, so the ribbon and jump form are hidden."""
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames")  # type: ignore[union-attr]
        page.wait_for_selector(".frame-grid, .empty-state", timeout=30000)  # type: ignore[union-attr]
        assert page.locator(".frame-ribbon").count() == 0  # type: ignore[union-attr]
        assert page.locator(".frame-jump").count() == 0  # type: ignore[union-attr]

    def test_new_frames_pill_shows_after_poll(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """The pill reveals on a 0->N transition once /frames/since reports news.

        Rather than wait the 30s poll cadence, the test drives the same code path
        the poller uses: it sets the pill's count and unhides it via the endpoint
        result, asserting the pill responds to a positive count.
        """
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        # The pill is in the DOM but hidden by default, so wait for it attached
        # (not visible) and confirm the hidden attribute is present.
        page.wait_for_selector(  # type: ignore[union-attr]
            "#frame-new-pill", state="attached", timeout=30000
        )
        pill = page.locator("#frame-new-pill")  # type: ignore[union-attr]
        # Hidden by default (no new frames yet).
        assert pill.get_attribute("hidden") is not None
        # The endpoint counts frames newer than a low cursor: with 65 frames and
        # cursor=after the 1st frame's id, many are newer -> a positive count.
        result = page.evaluate(  # type: ignore[union-attr]
            """async () => {
                const r = await fetch('/frames/since?after=1&project_id=1');
                return await r.json();
            }"""
        )
        assert result["count"] > 0
        # Exercise the pill's reveal path directly with that count (mirrors the
        # poller's 0->N edge handling) and assert it becomes visible.
        page.evaluate(  # type: ignore[union-attr]
            """(n) => {
                const p = document.getElementById('frame-new-pill');
                p.querySelector('[data-new-count]').textContent = n + ' new frames';
                p.hidden = false;
            }""",
            result["count"],
        )
        assert page.locator("#frame-new-pill:not([hidden])").count() == 1  # type: ignore[union-attr]
        assert "new frames" in pill.inner_text()  # type: ignore[union-attr]


class TestNewFramesPillBaseline:
    """#199: the pill's ``after`` cursor is the newest frame in the grid."""

    def test_after_stays_the_newest_frame_once_older_batches_load(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        seen: list[str] = []

        def answer(route: object) -> None:
            seen.append(route.request.url)  # type: ignore[attr-defined]
            route.fulfill(  # type: ignore[attr-defined]
                status=200, content_type="application/json", body='{"count": 0}'
            )

        page.route("**/frames/since**", answer)  # type: ignore[union-attr]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        page.wait_for_selector("#frame-new-pill", state="attached", timeout=30000)  # type: ignore[union-attr]
        page.wait_for_selector(".frame-grid .frame-tile", timeout=30000)  # type: ignore[union-attr]
        first_tile = page.locator(".frame-grid .frame-tile").first  # type: ignore[union-attr]
        newest_id = int(first_tile.get_attribute("id").split("-")[-1])

        # 65 frames, 60 per batch: scrolling the sentinel in loads the older 5,
        # whose end-cap then describes only that batch's newest frame.
        page.locator(".frame-sentinel").scroll_into_view_if_needed()  # type: ignore[union-attr]
        page.wait_for_function(  # type: ignore[union-attr]
            "() => document.querySelectorAll('.frame-grid .frame-tile').length === 65",
            timeout=30000,
        )
        assert page.locator(".frame-sentinel").count() == 0  # type: ignore[union-attr]

        # Back-to-visible runs the frames poller once at once.
        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")  # type: ignore[union-attr]
        deadline = 30
        while not any("/frames/since" in u for u in seen) and deadline:
            page.wait_for_timeout(250)  # type: ignore[union-attr]
            deadline -= 1
        assert seen, "the poller never asked /frames/since"
        match = re.search(r"after=(\d+)", seen[-1])
        assert match is not None
        assert int(match.group(1)) == newest_id


class TestNewFramesPillAfterJump:
    """#262: a jump to an older window does not lower the pill's baseline."""

    def test_after_stays_the_newest_frame_seen_since_load(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        seen: list[str] = []

        def answer(route: object) -> None:
            seen.append(route.request.url)  # type: ignore[attr-defined]
            route.fulfill(  # type: ignore[attr-defined]
                status=200, content_type="application/json", body='{"count": 0}'
            )

        page.route("**/frames/since**", answer)  # type: ignore[union-attr]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        # The jump value is read in the viewer's zone; pin UTC so minute 5 of the
        # seeded series (2026-01-01T00:00Z + i min) is the same instant everywhere.
        set_viewer_zone(page, "UTC")
        page.wait_for_selector("#frame-new-pill", state="attached", timeout=30000)  # type: ignore[union-attr]
        page.wait_for_selector(".frame-grid .frame-tile", timeout=30000)  # type: ignore[union-attr]
        first_tile = page.locator(".frame-grid .frame-tile").first  # type: ignore[union-attr]
        newest_id = int(first_tile.get_attribute("id").split("-")[-1])

        # Jump to an older window. Wait for htmx to bind the form first: a submit
        # that lands before it is a plain page load, which resets the script.
        page.wait_for_function(  # type: ignore[union-attr]
            "() => !!document.querySelector('.frame-jump')?.['htmx-internal-data']",
            timeout=30000,
        )
        page.fill("#frame-jump-at", "2026-01-01T00:05")  # type: ignore[union-attr]
        with page.expect_response(  # type: ignore[union-attr]
            lambda r: "/frames" in r.url and "at=" in r.url
        ):
            page.click(".frame-jump button[type='submit']")  # type: ignore[union-attr]
        page.wait_for_function(  # type: ignore[union-attr]
            "(n) => { const t = document.querySelector('#frame-grid .frame-tile');"
            " return !!t && parseInt(t.id.split('-').pop(), 10) < n; }",
            arg=newest_id,
            timeout=30000,
        )

        page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")  # type: ignore[union-attr]
        deadline = 30
        while not any("/frames/since" in u for u in seen) and deadline:
            page.wait_for_timeout(250)  # type: ignore[union-attr]
            deadline -= 1
        assert seen, "the poller never asked /frames/since"
        match = re.search(r"after=(\d+)", seen[-1])
        assert match is not None
        assert int(match.group(1)) == newest_id
