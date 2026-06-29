"""E2E for the screen-reader scroll announcement and the Settings tab order.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_scroll_page`` (Project id 1 seeded with 70 frames, crossing the
60-per-batch boundary).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestFramesScrollAnnounce:
    def test_scroll_announces_loaded_frames(
        self, logged_in_scroll_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_scroll_page  # type: ignore[misc]
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[union-attr]
        page.wait_for_selector(".frame-grid .frame-tile", timeout=30000)  # type: ignore[union-attr]
        # First batch: 60 tiles + a sentinel (70 seeded > 60); status is empty.
        assert page.locator(".frame-grid .frame-tile").count() == 60  # type: ignore[union-attr]
        status = page.locator("#frame-load-status")  # type: ignore[union-attr]
        assert status.count() == 1
        assert (status.inner_text() or "").strip() == ""

        # Scroll the sentinel into view -> hx-trigger="revealed" loads the rest
        # (10 more tiles + an end-cap) and the announcer updates the live region.
        page.locator(".frame-sentinel").scroll_into_view_if_needed()  # type: ignore[union-attr]
        page.wait_for_selector(".frame-end-cap", timeout=30000)  # type: ignore[union-attr]
        assert page.locator(".frame-grid .frame-tile").count() == 70  # type: ignore[union-attr]

        # The status region now mentions frames loaded (count > 0 branch). The
        # end-cap of this batch still carries rows, so it must NOT announce
        # "beginning reached".
        page.wait_for_function(  # type: ignore[union-attr]
            "document.getElementById('frame-load-status')"
            ".textContent.indexOf('frames loaded through') !== -1",
            timeout=30000,
        )
        text = page.locator("#frame-load-status").inner_text()  # type: ignore[union-attr]
        assert "10 frames loaded through" in text, text
        assert "Beginning" not in text, text


class TestSettingsTabOrder:
    def test_settings_tab_order_is_system_network_ldap_notifications_credentials(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/settings")  # type: ignore[union-attr]
        page.wait_for_selector(".tabs .tab-item", timeout=30000)  # type: ignore[union-attr]
        labels = page.locator(".tabs .tab-item").all_inner_texts()  # type: ignore[union-attr]
        labels = [s.strip() for s in labels]
        assert labels == [
            "System",
            "Network",
            "LDAP",
            "Notifications",
            "Credentials",
        ], labels
