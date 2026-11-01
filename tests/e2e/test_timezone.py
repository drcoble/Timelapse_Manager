"""E2E for the viewer's time zone (#70 F-R4, F-R5; from the audit's ``exp3.py`` D).

The browser runs in America/New_York (``browser_context_factory``) so it differs
from the zones the tests choose, which is the case the audit reproduced:

* a zone picked on Preferences survives later navigations (the browser shim no
  longer overwrites it);
* Preferences offers the browser's zone as a one-click switch;
* a failed save shows a toast and does not reload;
* a ribbon click pre-fills the date jump in the viewer's zone, and the jump
  reads it in that zone.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from tests.e2e._tz_support import local_input, set_viewer_zone, utc

pytestmark = pytest.mark.ui

_TIMEOUT = 30000
_BROWSER_TZ = "America/New_York"
_ALERT_TOAST = "#toast-region .toast[role=alert]"


def _ny_page(make_context: Callable[..., object]) -> object:
    """A signed-in page whose browser reports America/New_York."""
    context = make_context(timezone_id=_BROWSER_TZ)
    page = context.new_page()  # type: ignore[attr-defined]
    page.set_default_timeout(_TIMEOUT)
    return page


def _stored_zone(page: object) -> str | None:
    return page.evaluate("() => document.documentElement.dataset.viewerTz || null")  # type: ignore[attr-defined,no-any-return]


class TestPreferencesZone:
    def test_saved_zone_survives_navigation(
        self,
        logged_in_page: object,
        live_server: str,
        browser_context_factory: Callable[..., object],
    ) -> None:
        """Pick Europe/Paris in a New York browser: it is still Paris 3 pages on."""
        page = _ny_page(browser_context_factory)
        page.goto(f"{live_server}/account/preferences")  # type: ignore[attr-defined]
        # Start from an empty zone, so the shim's auto-fill races the save.
        set_viewer_zone(page, "")
        page.fill("#timezone-input", "Europe/Paris")  # type: ignore[attr-defined]
        with page.expect_navigation():  # type: ignore[attr-defined]
            page.click("#save-preferences-btn")  # type: ignore[attr-defined]
        assert page.input_value("#timezone-input") == "Europe/Paris"  # type: ignore[attr-defined]
        for path in ("/", "/events", "/account/preferences"):
            page.goto(f"{live_server}{path}")  # type: ignore[attr-defined]
            assert _stored_zone(page) == "Europe/Paris", path
        assert page.input_value("#timezone-input") == "Europe/Paris"  # type: ignore[attr-defined]

    def test_hint_offers_the_browser_zone(
        self,
        logged_in_page: object,
        live_server: str,
        browser_context_factory: Callable[..., object],
    ) -> None:
        """A stored zone that differs from the browser's shows a one-click hint."""
        page = _ny_page(browser_context_factory)
        page.goto(f"{live_server}/account/preferences")  # type: ignore[attr-defined]
        set_viewer_zone(page, "Europe/Paris")
        hint = page.locator("#timezone-browser-hint")  # type: ignore[attr-defined]
        hint.wait_for(state="visible")
        assert hint.locator("[data-browser-tz]").inner_text() == _BROWSER_TZ
        with page.expect_navigation():  # type: ignore[attr-defined]
            page.click("#timezone-use-browser-btn")  # type: ignore[attr-defined]
        assert page.input_value("#timezone-input") == _BROWSER_TZ  # type: ignore[attr-defined]
        assert _stored_zone(page) == _BROWSER_TZ
        assert page.locator("#timezone-browser-hint").is_hidden()  # type: ignore[attr-defined]

    def test_no_hint_when_zones_match(
        self,
        logged_in_page: object,
        live_server: str,
        browser_context_factory: Callable[..., object],
    ) -> None:
        page = _ny_page(browser_context_factory)
        page.goto(f"{live_server}/account/preferences")  # type: ignore[attr-defined]
        set_viewer_zone(page, _BROWSER_TZ)
        assert page.locator("#timezone-browser-hint").is_hidden()  # type: ignore[attr-defined]

    def test_failed_save_toasts_and_does_not_reload(
        self,
        logged_in_page: object,
        live_server: str,
        browser_context_factory: Callable[..., object],
    ) -> None:
        """A 500 from /account/timezone: a toast, no reload, the input kept."""
        page = _ny_page(browser_context_factory)
        page.goto(f"{live_server}/account/preferences")  # type: ignore[attr-defined]
        set_viewer_zone(page, "Europe/Paris")
        page.evaluate("() => { window.__notReloaded = true; }")  # type: ignore[attr-defined]
        page.route(  # type: ignore[attr-defined]
            "**/account/timezone",
            lambda route: route.fulfill(status=500, body="boom"),
        )
        page.fill("#timezone-input", "Asia/Tokyo")  # type: ignore[attr-defined]
        page.click("#save-preferences-btn")  # type: ignore[attr-defined]
        page.locator(_ALERT_TOAST).first.wait_for()  # type: ignore[attr-defined]
        assert page.evaluate("() => window.__notReloaded === true")  # type: ignore[attr-defined]
        assert page.input_value("#timezone-input") == "Asia/Tokyo"  # type: ignore[attr-defined]
        # The save button is usable again for a retry.
        assert page.locator("#save-preferences-btn").is_enabled()  # type: ignore[attr-defined]

    def test_unknown_zone_toasts_the_reason(
        self,
        logged_in_page: object,
        live_server: str,
        browser_context_factory: Callable[..., object],
    ) -> None:
        page = _ny_page(browser_context_factory)
        page.goto(f"{live_server}/account/preferences")  # type: ignore[attr-defined]
        set_viewer_zone(page, "Europe/Paris")
        page.evaluate("() => { window.__notReloaded = true; }")  # type: ignore[attr-defined]
        page.fill("#timezone-input", "Not/AZone")  # type: ignore[attr-defined]
        page.click("#save-preferences-btn")  # type: ignore[attr-defined]
        page.locator(_ALERT_TOAST, has_text="Unknown time zone").first.wait_for()  # type: ignore[attr-defined]
        assert page.evaluate("() => window.__notReloaded === true")  # type: ignore[attr-defined]
        page.reload()  # type: ignore[attr-defined]
        assert _stored_zone(page) == "Europe/Paris"


# The data server's project 1 has 65 frames one minute apart from 00:00Z.
_FRAME0 = utc(2026, 1, 1, 0, 0)
_FRAME20 = utc(2026, 1, 1, 0, 20)


class TestDateJumpInViewerZone:
    def test_ribbon_prefill_and_jump_use_the_viewer_zone(
        self,
        logged_in_data_page: tuple[object, str],
        browser_context_factory: Callable[..., object],
    ) -> None:
        """Stored Tokyo, browser New York: the prefill and the jump follow Tokyo.

        Tokyo separates the readings: 00:20Z is 09:20 there. Read as Tokyo, the
        jump windows around frame 20, which reaches frame 0; misread as UTC,
        09:20Z is past the series and the window is its tail, without frame 0.
        """
        _, base = logged_in_data_page  # type: ignore[misc]
        page = _ny_page(browser_context_factory)
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[attr-defined]
        set_viewer_zone(page, "Asia/Tokyo")
        page.wait_for_selector(".frame-ribbon .time-ribbon")  # type: ignore[attr-defined]
        page.evaluate(  # type: ignore[attr-defined]
            """(ms) => document.querySelector('.frame-ribbon').dispatchEvent(
                new CustomEvent('ribbon:jump',
                                {detail: {timestampMs: ms}, bubbles: true}))""",
            int(_FRAME20.timestamp() * 1000),
        )
        assert page.input_value("#frame-jump-at") == local_input(_FRAME20, "Asia/Tokyo")  # type: ignore[attr-defined]
        assert page.input_value("#frame-jump-at") == "2026-01-01T09:20"  # type: ignore[attr-defined]

        # A fresh newest-first page (frames 5..64) has no frame 0 tile.
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[attr-defined]
        page.wait_for_selector(".frame-grid .frame-tile")  # type: ignore[attr-defined]
        frame0 = f'#frame-grid .frame-tile[data-timestamp="{int(_FRAME0.timestamp())}"]'
        assert page.locator(frame0).count() == 0  # type: ignore[attr-defined]
        page.fill("#frame-jump-at", local_input(_FRAME20, "Asia/Tokyo"))  # type: ignore[attr-defined]
        page.click(".frame-jump button[type='submit']")  # type: ignore[attr-defined]
        page.locator(frame0).wait_for(state="attached")  # type: ignore[attr-defined]

    def test_jump_label_names_the_viewer_zone(
        self,
        logged_in_data_page: tuple[object, str],
        browser_context_factory: Callable[..., object],
    ) -> None:
        _, base = logged_in_data_page  # type: ignore[misc]
        page = _ny_page(browser_context_factory)
        page.goto(f"{base}/frames?project_id=1")  # type: ignore[attr-defined]
        set_viewer_zone(page, "Asia/Tokyo")
        label = page.locator("label[for='frame-jump-at']").inner_text()  # type: ignore[attr-defined]
        assert label.endswith("(Asia/Tokyo)")
