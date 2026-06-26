"""E2E for the audit log: the Operations/Audit tab bar and the audit log's
continuous-scroll append.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent. Uses
``logged_in_audit_page`` (90 seeded audit records crossing the audit batch
boundary) so the audit log shows a scroll sentinel that pages to an end-cap, and
``logged_in_events_page`` to confirm the tab bar sits on the operational page too.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestEventsAuditTabs:
    def test_admin_sees_operations_and_audit_tabs(
        self, logged_in_events_page: tuple[object, str]
    ) -> None:
        """An admin's events page shows both the Operations and Audit tabs."""
        page, base = logged_in_events_page  # type: ignore[misc]
        page.goto(f"{base}/events")  # type: ignore[union-attr]
        page.wait_for_selector(".tabs[role='tablist']", timeout=30000)  # type: ignore[union-attr]
        tabs = page.locator(".tabs[role='tablist'] .tab-item")  # type: ignore[union-attr]
        labels = tabs.all_inner_texts()
        assert "Operations" in labels
        assert "Audit" in labels
        # The Audit tab is a real route link to the audit page.
        audit_tab = page.locator(  # type: ignore[union-attr]
            ".tabs[role='tablist'] .tab-item[href='/events/audit']"
        )
        assert audit_tab.count() == 1


class TestAuditScroll:
    def test_audit_tab_navigates_and_scrolls(
        self, logged_in_audit_page: tuple[object, str]
    ) -> None:
        """Clicking the Audit tab lands on the audit log, which scrolls older rows."""
        page, base = logged_in_audit_page  # type: ignore[misc]
        page.goto(f"{base}/events")  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            ".tabs[role='tablist'] .tab-item[href='/events/audit']", timeout=30000
        )
        page.click(".tabs[role='tablist'] .tab-item[href='/events/audit']")  # type: ignore[union-attr]
        # Landed on the audit page (its tbody is present).
        page.wait_for_selector("#audit-tbody .event-row", timeout=30000)  # type: ignore[union-attr]
        # The active tab matches the current route: the Audit tab is selected and
        # the Operations tab is not (guards the exact-vs-startswith active logic).
        assert (
            page.locator(  # type: ignore[union-attr]
                ".tab-item[href='/events/audit'][aria-selected='true']"
            ).count()
            == 1
        )
        assert (
            page.locator(  # type: ignore[union-attr]
                ".tab-item[href='/events'][aria-selected='true']"
            ).count()
            == 0
        )
        first = page.locator("#audit-tbody .event-row").count()  # type: ignore[union-attr]
        assert first == 50  # first batch capped at the audit batch size
        # Scroll the sentinel into view -> the next (older) batch appends.
        page.locator("#audit-tbody .log-sentinel").scroll_into_view_if_needed()  # type: ignore[union-attr]
        page.wait_for_selector("#audit-tbody .log-end-cap", timeout=30000)  # type: ignore[union-attr]
        assert page.locator("#audit-tbody .event-row").count() > first  # type: ignore[union-attr]
