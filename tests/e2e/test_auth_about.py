"""E2E for the Phase-13 auth/about screen polish.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
The login paint test uses a fresh (unauthenticated) ``_chromium_page`` against
``live_server`` (an admin is seeded, so /login serves the form). The About
disclosure test uses ``logged_in_page``.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestLoginScreen:
    def test_meridian_mark_paints_and_theme_toggle_cycles(
        self, _chromium_page: object, live_server: str
    ) -> None:
        page = _chromium_page  # type: ignore[assignment]
        page.goto(f"{live_server}/login")  # type: ignore[union-attr]
        page.wait_for_selector(".login-logo svg", timeout=30000)  # type: ignore[union-attr]
        assert page.locator(".login-logo svg").is_visible()  # type: ignore[union-attr]
        # Pre-auth theme toggle cycles light → dark → system from the default.
        before = page.evaluate(  # type: ignore[union-attr]
            "document.documentElement.getAttribute('data-theme')"
        )
        page.click("#login-theme-toggle")  # type: ignore[union-attr]
        after = page.evaluate(  # type: ignore[union-attr]
            "document.documentElement.getAttribute('data-theme')"
        )
        assert after != before


class TestAboutScreen:
    def test_license_disclosure_toggles_open(
        self, logged_in_page: object, live_server: str
    ) -> None:
        page = logged_in_page  # type: ignore[assignment]
        page.goto(f"{live_server}/about")  # type: ignore[union-attr]
        page.wait_for_selector(".about-license-disclosure", timeout=30000)  # type: ignore[union-attr]
        disclosure = page.locator(".about-license-disclosure")  # type: ignore[union-attr]
        # Collapsed at rest → the <pre> is not visible.
        assert not page.locator(".about-license").is_visible()  # type: ignore[union-attr]
        page.locator(".about-license-disclosure > summary").click()  # type: ignore[union-attr]
        page.wait_for_selector(".about-license", state="visible", timeout=5000)  # type: ignore[union-attr]
        assert disclosure.get_attribute("open") is not None
