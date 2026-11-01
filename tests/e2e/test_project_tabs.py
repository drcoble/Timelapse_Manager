"""E2E: the project-detail Settings tab lazy-loads its form, and destructive
actions confirm inline rather than via a native dialog."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000

_DELETE_BUTTON = '[hx-get="/projects/1/delete-confirm"]'

# Postpones every DOMContentLoaded listener by 1.5 s. htmx binds its attributes
# (hx-get, ...) from such a listener, so this forces the window in which a page
# that has fired DOMContentLoaded is not yet interactive (#189).
_DELAY_DOM_CONTENT_LOADED = """
(() => {
  const original = document.addEventListener.bind(document);
  document.addEventListener = (type, listener, options) => {
    if (type !== "DOMContentLoaded") return original(type, listener, options);
    return original(type, (event) => setTimeout(() => listener(event), 1500), options);
  };
})();
"""


def _open_project_when_htmx_has_bound(page: object, base: str) -> None:
    """Load the project page and wait until htmx has processed the Delete button.

    ``goto(wait_until="domcontentloaded")`` returns once the event has fired, not
    once the listeners it ran have finished, so a click straight after it could
    land before htmx bound ``hx-get`` and do nothing. htmx marks each element it
    has processed with ``htmx-internal-data``; waiting for that is the event the
    click depends on.
    """
    page.goto(f"{base}/projects/1", wait_until="domcontentloaded")  # type: ignore[attr-defined]
    page.wait_for_function(  # type: ignore[attr-defined]
        "(selector) => { const el = document.querySelector(selector);"
        " return !!(el && el['htmx-internal-data']); }",
        arg=_DELETE_BUTTON,
        timeout=_TIMEOUT,
    )


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
        _open_project_when_htmx_has_bound(page, base)

        # Clicking Delete reveals the inline-confirm row (the real POST form).
        page.locator(_DELETE_BUTTON).click()  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            '#confirm-slot .inline-confirm form[action="/projects/1/delete"]',
            timeout=_TIMEOUT,
        )
        # Cancel collapses the slot. The Cancel button arrives in the swapped
        # fragment, so wait for htmx to bind it before clicking; a click that
        # lands first does nothing and the slot never clears (#189).
        page.wait_for_function(  # type: ignore[union-attr]
            "() => { const b = [...document.querySelectorAll('#confirm-slot button')]"
            ".find((el) => el.textContent.trim() === 'Cancel');"
            " return !!(b && b['htmx-internal-data']); }",
            timeout=_TIMEOUT,
        )
        page.locator("#confirm-slot").get_by_role(  # type: ignore[union-attr]
            "button", name="Cancel"
        ).click()
        page.wait_for_function(  # type: ignore[union-attr]
            "() => document.querySelector('#confirm-slot').children.length === 0",
            timeout=_TIMEOUT,
        )

    def test_delete_confirm_survives_a_late_htmx_bind(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """With htmx binding 1.5 s after DOMContentLoaded the flow still works."""
        page, base = logged_in_data_page  # type: ignore[misc]
        page.add_init_script(_DELAY_DOM_CONTENT_LOADED)  # type: ignore[union-attr]
        _open_project_when_htmx_has_bound(page, base)

        page.locator(_DELETE_BUTTON).click()  # type: ignore[union-attr]
        page.wait_for_selector(  # type: ignore[union-attr]
            '#confirm-slot .inline-confirm form[action="/projects/1/delete"]',
            timeout=_TIMEOUT,
        )
