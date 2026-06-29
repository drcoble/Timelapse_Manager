"""E2E theme smoke — verifies the Meridian palette + Geist font are live.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent
(via the deferred-import ``_chromium_page`` fixture). No module-level
Playwright import so default collection stays green.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def _font_family(page: object) -> str:
    return page.evaluate(  # type: ignore[union-attr]
        "getComputedStyle(document.body).fontFamily"
    )


def _bg(page: object) -> str:
    return page.evaluate(  # type: ignore[union-attr]
        "getComputedStyle(document.body).backgroundColor"
    )


class TestThemeTokens:
    def test_body_font_resolves_to_geist(
        self, _chromium_page: object, live_server: str
    ) -> None:
        page = _chromium_page  # type: ignore[assignment]
        page.goto(f"{live_server}/login")  # type: ignore[union-attr]
        assert "Geist" in _font_family(page)

    def test_dark_theme_uses_indigo_background(
        self, _chromium_page: object, live_server: str
    ) -> None:
        page = _chromium_page  # type: ignore[assignment]
        page.goto(f"{live_server}/login")  # type: ignore[union-attr]
        page.evaluate(  # type: ignore[union-attr]
            "document.documentElement.setAttribute('data-theme','dark')"
        )
        # #0c0d14 -> rgb(12, 13, 20)
        assert _bg(page) == "rgb(12, 13, 20)"

    def test_light_theme_uses_linen_background(
        self, _chromium_page: object, live_server: str
    ) -> None:
        page = _chromium_page  # type: ignore[assignment]
        page.goto(f"{live_server}/login")  # type: ignore[union-attr]
        page.evaluate(  # type: ignore[union-attr]
            "document.documentElement.setAttribute('data-theme','light')"
        )
        # #f5f4f0 -> rgb(245, 244, 240)
        assert _bg(page) == "rgb(245, 244, 240)"
