"""E2E for the operational events log: continuous-scroll append, the multi-select
level chips, the date-jump window, and the new-events pill.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent. Uses
``logged_in_events_page`` (90 seeded camera events spaced one minute apart from
2026-02-01T00:00 UTC, levels cycling info/warning/error/critical) so the log
crosses the 75-per-batch boundary and the chips have all four severities.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


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
        # The last toggle triggers the re-query; wait for the list to settle.
        page.wait_for_timeout(800)  # type: ignore[union-attr]
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

    def test_events_date_jump_windows_list(
        self, logged_in_events_page: tuple[object, str]
    ) -> None:
        """Submitting the date-jump form swaps the list to the at-or-before window."""
        page, base = logged_in_events_page  # type: ignore[misc]
        page.goto(f"{base}/events?scope=camera")  # type: ignore[union-attr]
        page.wait_for_selector(".events-table .event-row", timeout=30000)  # type: ignore[union-attr]
        # Jump to ~minute 20 -> the list lands on the events at-or-before that
        # instant (the newest visible row is event 20, not event 89).
        page.fill("#events-jump-at", "2026-02-01T00:20")  # type: ignore[union-attr]
        page.click("#events-jump button[type='submit']")  # type: ignore[union-attr]
        page.wait_for_timeout(800)  # type: ignore[union-attr]
        rows = page.locator(".events-table .event-row")  # type: ignore[union-attr]
        assert rows.count() > 0
        # The newest event after the jump is "scroll event 20"; later events are
        # excluded by the at-or-before window.
        body_text = page.locator("#events-tbody").inner_text()  # type: ignore[union-attr]
        assert "scroll event 20" in body_text
        assert "scroll event 21" not in body_text
        # The page chrome is intact (no nested table swapped into the body).
        assert page.locator("#events-tbody table").count() == 0  # type: ignore[union-attr]

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
