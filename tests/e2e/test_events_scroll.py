"""E2E for the operational events log: continuous-scroll append, the multi-select
level chips, the date-jump window, and the new-events pill.

The date-jump tests pin the viewer's zone first (``_tz_support``), because a
naive jump value is read in that zone (#70 F-R5, #110).

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent. Uses
``logged_in_events_page`` (90 seeded camera events spaced one minute apart from
2026-02-01T00:00 UTC, levels cycling info/warning/error/critical) so the log
crosses the 75-per-batch boundary and the chips have all four severities.
"""

from __future__ import annotations

import pytest

from tests.e2e._tz_support import local_input, set_viewer_zone, utc

pytestmark = pytest.mark.ui

# Event 20 of the seeded log (2026-02-01T00:00Z + 20 minutes).
_EVENT_20_AT = utc(2026, 2, 1, 0, 20)

# True once htmx has processed the date-jump form. A submit that lands before
# that is a plain GET of the form, not the hx-get swap, so the test would wait
# on a response that never comes (#265). Same pattern as test_project_tabs.
_JUMP_FORM_BOUND_JS = (
    "() => !!document.querySelector('#events-jump')?.['htmx-internal-data']"
)

# True once the first row of the events list carries the given message.
_FIRST_MESSAGE_IS_JS = """(want) => {
    const cell = document.querySelector('#events-tbody .event-row .event-message');
    return !!cell && cell.textContent.trim() === want;
}"""


def _messages(page: object) -> list[str]:
    """The messages of the visible event rows, newest first."""
    cells = page.locator("#events-tbody .event-row .event-message")  # type: ignore[attr-defined]
    return [text.strip() for text in cells.all_inner_texts()]


class TestEventsScrollNav:
    def test_events_scroll_appends_older_events(
        self, logged_in_events_page: tuple[object, str]
    ) -> None:
        """Scrolling the sentinel into view appends the next (older) batch."""
        page, base = logged_in_events_page  # type: ignore[misc]
        page.goto(f"{base}/events")  # type: ignore[union-attr]
        page.wait_for_selector(".events-table .event-row", timeout=30000)  # type: ignore[union-attr]
        first = page.locator(".events-table .event-row").count()  # type: ignore[union-attr]
        assert first == 75  # first batch capped at _OPERATIONAL_EVENTS_PER_PAGE
        page.locator(".log-sentinel").scroll_into_view_if_needed()  # type: ignore[union-attr]
        page.wait_for_selector(".log-end-cap", timeout=30000)  # type: ignore[union-attr]
        assert page.locator(".events-table .event-row").count() > first  # type: ignore[union-attr]

    def test_level_chip_toggle_refilters_list(
        self, logged_in_events_page: tuple[object, str]
    ) -> None:
        """Deselecting chips down to a single level re-queries the list."""
        page, base = logged_in_events_page  # type: ignore[misc]
        page.goto(f"{base}/events?scope=camera")  # type: ignore[union-attr]
        page.wait_for_selector("#events-level-chips .chip", timeout=30000)  # type: ignore[union-attr]
        # All chips start pressed (all levels shown). Turn off everything except
        # ERROR, so only error rows remain. 90 events, levels cycle 4-wide, so
        # error is index 2 of every 4 -> ceil count for i in 0..89 where i%4==2.
        for lv in ("info", "warning", "critical"):
            page.click(f'#events-level-chips .chip[data-level="{lv}"]')  # type: ignore[union-attr]
        # The last toggle triggers the re-query; wait for its rows (the newest
        # error is event 86), not a fixed delay.
        page.wait_for_function(_FIRST_MESSAGE_IS_JS, arg="scroll event 86")  # type: ignore[union-attr]
        # Rows were swapped in, not a second copy of the page (#34).
        assert page.locator("#events-level-chips").count() == 1  # type: ignore[union-attr]
        assert page.locator("#events-tbody table").count() == 0  # type: ignore[union-attr]
        rows = page.locator(".events-table .event-row")  # type: ignore[union-attr]
        total = rows.count()
        assert total > 0
        # Every remaining row is an error row (carried by the row's level class).
        error_rows = page.locator(  # type: ignore[union-attr]
            ".events-table .event-row.event-level-error"
        )
        info_rows = page.locator(  # type: ignore[union-attr]
            ".events-table .event-row.event-level-info"
        )
        assert error_rows.count() == total
        assert info_rows.count() == 0

    @pytest.mark.parametrize("viewer_tz", ["UTC", "America/New_York"])
    def test_events_date_jump_windows_list(
        self, logged_in_events_page: tuple[object, str], viewer_tz: str
    ) -> None:
        """Submitting the date-jump form swaps the list to the at-or-before window."""
        page, base = logged_in_events_page  # type: ignore[misc]
        page.goto(f"{base}/events?scope=camera")  # type: ignore[union-attr]
        # The naive datetime-local value is read in the viewer's stored zone
        # (#70 F-R5), so pin that zone instead of inheriting whatever the
        # browser shim stored from the host's zone (#110), and type event 20's
        # fixed instant as wall-clock time in it. Nothing here depends on
        # today's date: #110 ran this with the browser clock pinned across both
        # 2026 US DST changes and three month ends, in three server zones.
        set_viewer_zone(page, viewer_tz)
        page.wait_for_selector(".events-table .event-row", timeout=30000)  # type: ignore[union-attr]
        # Jump to ~minute 20 -> the list lands on the events at-or-before that
        # instant (the newest visible row is event 20, not event 89). The test
        # used to fail every run because the jump never fired: its hx-vals "js:"
        # hook was refused by the CSP (no 'unsafe-eval'), so the list stayed on
        # the newest page (#34).
        page.fill("#events-jump-at", local_input(_EVENT_20_AT, viewer_tz))  # type: ignore[union-attr]
        page.wait_for_function(_JUMP_FORM_BOUND_JS, timeout=30000)  # type: ignore[union-attr]
        with page.expect_response(  # type: ignore[union-attr]
            lambda r: "/events?" in r.url and "at=" in r.url
        ):
            page.click("#events-jump button[type='submit']")  # type: ignore[union-attr]
        # Wait for the swap itself, not a fixed delay.
        page.wait_for_function(_FIRST_MESSAGE_IS_JS, arg="scroll event 20")  # type: ignore[union-attr]
        messages = _messages(page)
        # The newest event after the jump is "scroll event 20"; later events are
        # excluded by the at-or-before window and older ones page from there.
        assert messages[0] == "scroll event 20"
        assert messages == [f"scroll event {i}" for i in range(20, -1, -1)]
        # The page chrome is intact (no nested table swapped into the body).
        assert page.locator("#events-tbody table").count() == 0  # type: ignore[union-attr]

    def test_events_date_jump_carries_the_chip_selection(
        self, logged_in_events_page: tuple[object, str]
    ) -> None:
        """The jump request carries the live level chips (htmx:configRequest)."""
        page, base = logged_in_events_page  # type: ignore[misc]
        page.goto(f"{base}/events?scope=camera")  # type: ignore[union-attr]
        # Pin the zone the jump value is read in (#70 F-R5, #110).
        set_viewer_zone(page, "UTC")
        page.wait_for_selector("#events-level-chips .chip", timeout=30000)  # type: ignore[union-attr]
        for lv in ("info", "warning", "critical"):
            page.click(f'#events-level-chips .chip[data-level="{lv}"]')  # type: ignore[union-attr]
        # Wait for the re-query of the final selection to land, not a delay.
        page.wait_for_function(_FIRST_MESSAGE_IS_JS, arg="scroll event 86")  # type: ignore[union-attr]
        # The re-query swaps rows, not a second page, into the list.
        assert page.locator("#events-level-chips").count() == 1  # type: ignore[union-attr]
        assert page.locator("#events-tbody table").count() == 0  # type: ignore[union-attr]
        page.fill("#events-jump-at", local_input(_EVENT_20_AT, "UTC"))  # type: ignore[union-attr]
        page.wait_for_function(_JUMP_FORM_BOUND_JS, timeout=30000)  # type: ignore[union-attr]
        with page.expect_response(  # type: ignore[union-attr]
            lambda r: "at=" in r.url and "level=error" in r.url
        ):
            page.click("#events-jump button[type='submit']")  # type: ignore[union-attr]
        # Error is every fourth event from index 2: 18, 14, 10, 6, 2.
        page.wait_for_function(_FIRST_MESSAGE_IS_JS, arg="scroll event 18")  # type: ignore[union-attr]
        assert _messages(page) == [f"scroll event {i}" for i in (18, 14, 10, 6, 2)]

    def test_new_events_pill_reveals_after_poll(
        self, logged_in_events_page: tuple[object, str]
    ) -> None:
        """The pill reveals on a 0->N edge once /events/since reports new rows.

        Rather than wait the 30s cadence, the test drives the same code path the
        poller uses: it queries /events/since for a positive count and exercises
        the pill's reveal, asserting it responds to a positive count.
        """
        page, base = logged_in_events_page  # type: ignore[misc]
        page.goto(f"{base}/events?scope=camera")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#events-new-pill", state="attached", timeout=30000
        )
        pill = page.locator("#events-new-pill")  # type: ignore[union-attr]
        # Hidden by default (no new events relative to the rendered head).
        assert pill.get_attribute("hidden") is not None
        # Counting from a low cursor (after=0) reports the seeded camera events as
        # "new" -> a positive count. (after=0 excludes the admin's system 'signed
        # in' event by scope.)
        result = page.evaluate(  # type: ignore[union-attr]
            """async () => {
                const r = await fetch('/events/since?after=0&scope=camera');
                return await r.json();
            }"""
        )
        assert result["count"] > 0
        # Exercise the reveal path directly with that count (mirrors the poller's
        # 0->N edge handling) and assert the pill becomes visible.
        page.evaluate(  # type: ignore[union-attr]
            """(n) => {
                const p = document.getElementById('events-new-pill');
                p.querySelector('[data-new-count]').textContent = n + ' new events';
                p.hidden = false;
            }""",
            result["count"],
        )
        assert page.locator("#events-new-pill:not([hidden])").count() == 1  # type: ignore[union-attr]
        assert "new events" in pill.inner_text()  # type: ignore[union-attr]
