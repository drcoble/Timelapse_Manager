"""E2E: "Report a problem" (#8) in a real browser.

Opens the drawer from a project page (prefilled project and area), attaches a
screenshot, submits, sees the report id, and finds it in Settings > Problem
reports. Also checks the full-page fallback works with JavaScript disabled.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.reports_support import PNG_BYTES

pytestmark = pytest.mark.ui

_TIMEOUT = 30_000
_KEY = re.compile(r"TLM-\d{8}-\d{4}")
_DRAWER_OPEN_JS = (
    "() => document.querySelector('#drawer-main')"
    ".getAttribute('aria-hidden') === 'false'"
)


_FOOTER_LINK = ".app-footer a[hx-get]"


def _wait_for_htmx_on_footer_link(page: object) -> None:
    """Wait until htmx has processed the footer's "Report a problem" link.

    ``goto(wait_until="domcontentloaded")`` returns once the event has fired, not
    once htmx (which binds ``hx-get`` from a DOMContentLoaded listener) has
    finished. A click that lands first follows the link's plain ``href`` to the
    full-page fallback and the drawer never opens (the #189 / #270 pattern).
    """
    page.wait_for_function(  # type: ignore[attr-defined]
        "(selector) => { const el = document.querySelector(selector);"
        " return !!(el && el['htmx-internal-data']); }",
        arg=_FOOTER_LINK,
        timeout=_TIMEOUT,
    )


def test_file_report_from_project_drawer(
    logged_in_data_page: tuple[object, str], tmp_path: Path
) -> None:
    page, base = logged_in_data_page  # type: ignore[misc]
    shot = tmp_path / "shot.png"
    shot.write_bytes(PNG_BYTES)

    page.goto(f"{base}/projects/1", wait_until="domcontentloaded")  # type: ignore[union-attr]
    _wait_for_htmx_on_footer_link(page)
    page.locator(".app-footer a", has_text="Report a problem").click()  # type: ignore[union-attr]
    page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]

    drawer = page.locator("#drawer-main")  # type: ignore[union-attr]
    assert page.locator("#drawer-title").inner_text() == "Report a problem"  # type: ignore[union-attr]
    assert drawer.locator("#report-area").input_value() == "project"
    assert drawer.locator('input[name="project_id"]').input_value() == "1"

    drawer.locator("#report-title").fill("Roof capture stalled")
    drawer.locator("#report-description").fill("No frames since noon.")
    drawer.locator("#report-attachment").set_input_files(str(shot))
    drawer.locator("[data-report-submit]").click()

    status = drawer.locator("#report-form-container[role=status]")
    status.wait_for(timeout=_TIMEOUT)
    match = _KEY.search(status.inner_text())
    assert match is not None
    key = match.group(0)

    page.goto(f"{base}/settings/reports", wait_until="domcontentloaded")  # type: ignore[union-attr]
    row = page.locator("tr", has_text=key)  # type: ignore[union-attr]
    assert "Roof capture stalled" in row.inner_text()
    assert "saved: ok" in row.inner_text().lower()


def test_settings_tab_loads_report_table(logged_in_page: object) -> None:
    page = logged_in_page
    base = page.url.split("/", 3)[:3]  # type: ignore[union-attr]
    page.goto("/".join(base) + "/settings", wait_until="domcontentloaded")  # type: ignore[union-attr]
    page.locator('[data-tab-target="#tab-reports"]').click()  # type: ignore[union-attr]
    page.locator(
        "#tab-reports", has_text="No problem reports have been filed."
    ).wait_for(  # type: ignore[union-attr]
        timeout=_TIMEOUT
    )


def test_no_js_full_page_fallback(
    logged_in_data_page: tuple[object, str],
) -> None:
    page, base = logged_in_data_page  # type: ignore[misc]
    context = page.context  # type: ignore[union-attr]
    nojs = context.browser.new_context(java_script_enabled=False)
    nojs.add_cookies(context.cookies())
    try:
        tab = nojs.new_page()
        tab.goto(f"{base}/about", wait_until="domcontentloaded")
        tab.locator(".app-footer a", has_text="Report a problem").click()
        tab.wait_for_url(re.compile(r"/report\?page="))
        tab.locator("#report-title").fill("No-JS report")
        tab.locator("[data-report-submit]").click()
        tab.locator("h1", has_text="Report filed").wait_for(timeout=_TIMEOUT)
        assert _KEY.search(tab.content()) is not None
    finally:
        nojs.close()
