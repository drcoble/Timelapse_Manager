"""E2E for the Settings tab-bar and the Account-preferences segmented control.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_page`` (admin) + ``live_server`` — Settings and Preferences
are admin-reachable.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestSettingsTabs:
    def test_switching_tabs_toggles_panels(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/settings")  # type: ignore[union-attr]
        page.wait_for_selector(".tabs[role='tablist']", timeout=30000)  # type: ignore[union-attr]
        system = page.locator("#tab-system")  # type: ignore[union-attr]
        notifications = page.locator("#tab-notifications")  # type: ignore[union-attr]
        # System is the default-visible panel.
        assert system.is_visible()
        assert not notifications.is_visible()
        # Click the Notifications tab → panels swap.
        page.click('.tab-item[data-tab-target="#tab-notifications"]')  # type: ignore[union-attr]
        page.wait_for_selector("#tab-notifications", state="visible", timeout=5000)  # type: ignore[union-attr]
        assert notifications.is_visible()
        assert not system.is_visible()
        # The clicked tab is marked selected.
        assert (
            page.locator(
                '.tab-item[data-tab-target="#tab-notifications"]'
            ).get_attribute("aria-selected")  # type: ignore[union-attr]
            == "true"
        )

    def test_ldap_and_credentials_panels_reachable(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/settings")  # type: ignore[union-attr]
        page.click('.tab-item[data-tab-target="#tab-ldap"]')  # type: ignore[union-attr]
        page.wait_for_selector("#ldap-settings-form", state="visible", timeout=5000)  # type: ignore[union-attr]
        page.click('.tab-item[data-tab-target="#tab-credentials"]')  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            "#camera-credentials-form", state="visible", timeout=5000
        )


class TestThemeSegmentedControl:
    def test_segment_click_applies_theme_live(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/account/preferences")  # type: ignore[union-attr]
        page.wait_for_selector("#theme-segment", timeout=30000)  # type: ignore[union-attr]
        # The radio is visually hidden; users click the segment label. Clicking
        # the label checks the radio and fires the change handler.
        page.locator(  # type: ignore[union-attr]
            "#theme-segment label:has(input[value='light'])"
        ).click()
        assert (
            page.evaluate(  # type: ignore[union-attr]
                "document.documentElement.getAttribute('data-theme')"
            )
            == "light"
        )
        # Pick "system" → the attribute is removed (follow OS).
        page.locator(  # type: ignore[union-attr]
            "#theme-segment label:has(input[value='system'])"
        ).click()
        assert (
            page.evaluate(  # type: ignore[union-attr]
                "document.documentElement.getAttribute('data-theme')"
            )
            is None
        )
