"""E2E icon smoke — the sprite is present and a referenced symbol paints.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
No module-level Playwright import so default collection stays green.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Seeded by the e2e ``live_server`` fixture (see conftest ``_seed_admin``).
_ADMIN_USER = "admin"
_ADMIN_PASS = "AdminP@ssw0rd1234"


def _login(page: object, base_url: str) -> None:
    """Log in as the seeded admin, robust to a cold-server first-request race.

    On a slow/cold CI runner the very first login POST can land before the
    freshly-booted server is fully ready and silently re-render the login form
    (page stays on /login) rather than redirecting to the shell. We submit the
    form and, if the authenticated shell hasn't appeared, retry the whole
    login once. This helper is the foundation for every authenticated e2e test.
    """
    page.set_default_timeout(60000)  # type: ignore[union-attr]

    def _attempt(nav_timeout: int) -> bool:
        try:
            page.goto(  # type: ignore[union-attr]
                f"{base_url}/login", wait_until="domcontentloaded"
            )
            # A prior slow attempt may have authenticated us already, in which
            # case /login redirects straight to the shell.
            if page.locator(".app-shell").count() >= 1:  # type: ignore[union-attr]
                return True
            page.wait_for_selector(  # type: ignore[union-attr]
                "input[name='username']", timeout=nav_timeout
            )
            page.fill("input[name='username']", _ADMIN_USER)  # type: ignore[union-attr]
            page.fill("input[name='password']", _ADMIN_PASS)  # type: ignore[union-attr]
            page.click("button[type='submit']")  # type: ignore[union-attr]
            page.wait_for_selector(".app-shell", timeout=nav_timeout)  # type: ignore[union-attr]
            return True
        except Exception:
            return False

    # First attempt with a generous timeout; one retry if login didn't take.
    if not _attempt(30000):
        assert _attempt(30000), "login did not reach the authenticated shell"


class TestIconSprite:
    def test_sprite_present_on_shell(
        self, _chromium_page: object, live_server: str
    ) -> None:
        page = _chromium_page  # type: ignore[assignment]
        _login(page, live_server)
        assert page.locator(".icon-sprite").count() >= 1  # type: ignore[union-attr]

    def test_header_logo_mark_paints(
        self, _chromium_page: object, live_server: str
    ) -> None:
        page = _chromium_page  # type: ignore[assignment]
        _login(page, live_server)
        use = page.locator('.header-logo-icon use[href="#icon-logo"]')  # type: ignore[union-attr]
        assert use.count() == 1
        # The referenced symbol resolves to a non-zero painted box.
        box = page.locator(".header-logo-icon svg.icon").bounding_box()  # type: ignore[union-attr]
        assert box is not None and box["width"] > 0 and box["height"] > 0
