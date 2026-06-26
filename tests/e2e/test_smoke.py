"""E2E smoke tests — real Chromium browser hits the live app.

All tests in this module require the ``ui`` marker and are skipped
automatically when Playwright browsers are not installed.

Collection note: no module-level Playwright imports are present.  The import
happens inside the ``_chromium_page`` fixture (deferred, function scope) so
pytest can collect this file without the package being present -- and the
default ``pytest -n auto`` run stays green.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


class TestLoginPage:
    def test_login_page_renders(self, _chromium_page: object, live_server: str) -> None:
        """The /login route returns a page with the expected title."""
        # Playwright page type hint is deferred; we rely on duck-typing here so
        # no top-level import is needed.
        page = _chromium_page  # type: ignore[assignment]
        page.goto(f"{live_server}/login")  # type: ignore[union-attr]
        assert "Sign In" in page.title()  # type: ignore[union-attr]

    def test_login_page_has_username_field(
        self, _chromium_page: object, live_server: str
    ) -> None:
        """The /login page renders an input with name='username'."""
        page = _chromium_page  # type: ignore[assignment]
        page.goto(f"{live_server}/login")  # type: ignore[union-attr]
        locator = page.locator("input[name='username']")  # type: ignore[union-attr]
        assert locator.count() == 1

    def test_login_page_has_password_field(
        self, _chromium_page: object, live_server: str
    ) -> None:
        """The /login page renders an input with name='password' of type password."""
        page = _chromium_page  # type: ignore[assignment]
        page.goto(f"{live_server}/login")  # type: ignore[union-attr]
        locator = page.locator("input[name='password'][type='password']")  # type: ignore[union-attr]
        assert locator.count() == 1

    def test_login_page_has_submit_button(
        self, _chromium_page: object, live_server: str
    ) -> None:
        """The /login page renders a submit button (Sign In)."""
        page = _chromium_page  # type: ignore[assignment]
        page.goto(f"{live_server}/login")  # type: ignore[union-attr]
        locator = page.locator("button[type='submit']")  # type: ignore[union-attr]
        assert locator.count() >= 1

    def test_login_page_heading_text(
        self, _chromium_page: object, live_server: str
    ) -> None:
        """The /login page renders an h1 containing 'Timelapse Manager'."""
        page = _chromium_page  # type: ignore[assignment]
        page.goto(f"{live_server}/login")  # type: ignore[union-attr]
        heading = page.locator("h1")  # type: ignore[union-attr]
        assert heading.count() >= 1
        assert "Timelapse Manager" in heading.first.inner_text()  # type: ignore[union-attr]
