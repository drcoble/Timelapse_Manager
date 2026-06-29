"""E2E: the project-detail Settings tab lazy-loads its form, and destructive
actions confirm inline rather than via a native dialog."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000


class TestProjectDetailTabs:
    def test_settings_tab_lazy_loads_the_form(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1", wait_until="domcontentloaded")  # type: ignore[union-attr]

        # Click the Settings tab; the form is lazy-loaded into its panel.
        page.locator('[data-tab-target="#tab-settings"]').click()  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            '#tab-settings form[action="/projects/1/settings"]', timeout=_TIMEOUT
        )
        assert (
            page.locator(  # type: ignore[union-attr]
                '#tab-settings [name="capture_interval_value"]'
            ).count()
            >= 1
        )

    def test_delete_confirms_inline_without_native_dialog(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]
        page.goto(f"{base}/projects/1", wait_until="domcontentloaded")  # type: ignore[union-attr]

        # Clicking Delete reveals the inline-confirm row (the real POST form).
        page.locator('[hx-get="/projects/1/delete-confirm"]').click()  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            '#confirm-slot .inline-confirm form[action="/projects/1/delete"]',
            timeout=_TIMEOUT,
        )
        # Cancel collapses the slot.
        page.locator("#confirm-slot").get_by_role(  # type: ignore[union-attr]
            "button", name="Cancel"
        ).click()
        page.wait_for_function(  # type: ignore[union-attr]
            "() => document.querySelector('#confirm-slot').children.length === 0",
            timeout=_TIMEOUT,
        )
