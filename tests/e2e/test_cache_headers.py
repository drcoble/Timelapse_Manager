"""E2E for static versioning and dynamic cache headers (#70 F-S2, F-R8, F-P5).

* After sign-out, Back does not redisplay an authenticated page: the pages were
  served ``no-store``, so the browser re-requests them and is sent to login.
* Every static request a page makes carries ``?v=``, on first and repeat
  navigation.
* The in-memory CSS bundle styles the page exactly as the ``@import`` chain it
  replaces (same computed styles on a sample of elements).

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.ui


def _sign_out(page: Any) -> None:
    # The Sign out button sits in a hover dropdown; submit its form directly.
    with page.expect_navigation():
        page.evaluate("document.querySelector('form[action=\"/logout\"]').submit()")
    page.wait_for_selector("input[name='username']")


class TestBackAfterSignOut:
    def test_back_does_not_show_an_authenticated_page(
        self, logged_in_data_page: tuple[Any, str]
    ) -> None:
        page, base = logged_in_data_page
        page_headers: dict[str, str] = {}

        def _record(response: Any) -> None:
            if response.request.resource_type == "document":
                page_headers[response.url] = response.headers.get("cache-control", "")

        page.on("response", _record)
        page.goto(f"{base}/projects/1", wait_until="networkidle")
        page.goto(f"{base}/about", wait_until="networkidle")
        assert page.locator(".app-shell").count() == 1
        assert page_headers[f"{base}/projects/1"] == "no-store"
        assert page_headers[f"{base}/about"] == "no-store"

        _sign_out(page)
        page.go_back(wait_until="load")

        # Checked at once, not waited for: a page restored from history would
        # still show the shell here (until its next poll 401s and redirects).
        # With no-store the browser re-requested /about without a session and
        # landed on the login form instead.
        assert page.locator(".app-shell").count() == 0
        assert "/login" in page.url
        assert page.locator("input[name='username']").count() == 1


class TestStaticRequestsAreVersioned:
    def test_every_static_request_carries_v(
        self, logged_in_data_page: tuple[Any, str]
    ) -> None:
        page, base = logged_in_data_page
        static_urls: list[str] = []
        page.on(
            "request",
            lambda request: (
                static_urls.append(request.url) if "/static/" in request.url else None
            ),
        )
        for _ in range(2):
            page.goto(f"{base}/projects/1")
            page.wait_for_selector(".app-shell")
            page.wait_for_load_state("networkidle")

        assert static_urls, "no static requests observed"
        unversioned = [url for url in static_urls if "?v=" not in url]
        assert unversioned == []
        # The stylesheet is the in-memory bundle, not the @import index.
        assert any("/static/css/app.bundle.css?v=" in url for url in static_urls)
        assert not any("/static/css/app.css" in url for url in static_urls)


# Elements across the cascade: tokens/base, layout, components, screens and the
# responsive overrides that must win by source order.
_SELECTORS = ["body", ".app-shell", "header", "nav", "main", "h1", "a", "button"]

_COMPUTED_STYLES_JS = """
(selectors) => {
  const props = ['color', 'background-color', 'font-family', 'font-size',
                 'font-weight', 'display', 'padding', 'margin', 'border',
                 'grid-template-columns', 'gap', 'line-height'];
  const out = {};
  for (const sel of selectors) {
    const el = document.querySelector(sel);
    if (!el) continue;
    const cs = getComputedStyle(el);
    out[sel] = props.map((p) => p + ':' + cs.getPropertyValue(p)).join(';');
  }
  return out;
}
"""

_SWAP_TO_IMPORT_CHAIN_JS = """
() => new Promise((resolve, reject) => {
  const bundle = document.querySelector('link[rel="stylesheet"]');
  const raw = document.createElement('link');
  raw.rel = 'stylesheet';
  raw.href = '/static/css/app.css';
  raw.onload = () => { bundle.remove(); resolve(true); };
  raw.onerror = () => reject(new Error('app.css failed to load'));
  document.head.appendChild(raw);
})
"""


class TestBundleMatchesImportChain:
    @pytest.mark.parametrize("width", [1280, 500])
    def test_computed_styles_identical(
        self, logged_in_data_page: tuple[Any, str], width: int
    ) -> None:
        page, base = logged_in_data_page
        page.set_viewport_size({"width": width, "height": 900})
        page.goto(f"{base}/projects/1")
        page.wait_for_selector(".app-shell")
        bundled = page.evaluate(_COMPUTED_STYLES_JS, _SELECTORS)
        assert len(bundled) >= 5
        assert "Geist" in bundled["body"]  # the stylesheet really applied

        page.evaluate(_SWAP_TO_IMPORT_CHAIN_JS)
        chained = page.evaluate(_COMPUTED_STYLES_JS, _SELECTORS)

        assert chained == bundled
