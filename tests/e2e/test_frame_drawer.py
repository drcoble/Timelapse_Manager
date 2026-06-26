"""E2E tests for the frame-detail drawer.

Covers: opening the drawer from a frame tile, navigating to a neighbour frame
via the prev/next controls (which re-swap the drawer body in place), and the
no-JS direct-GET fallback rendering a full page.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
Uses ``logged_in_data_page`` (seeds project id 1 with frames).
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui

# Timeout in ms for async operations (HTMX swaps, CSS transitions, focus).
_TIMEOUT = 30_000

_DRAWER_OPEN_JS = (
    "() => document.querySelector('#drawer-main')"
    ".getAttribute('aria-hidden') === 'false'"
)


class TestFrameDrawerLifecycle:
    """Opening the drawer from a frame tile and navigating between frames."""

    def test_clicking_frame_tile_opens_drawer(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """Clicking a frame tile loads the detail fragment and opens the drawer
        with a 'Frame #...' title."""
        page, base = logged_in_data_page  # type: ignore[misc]

        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )

        # The tile thumbnail is the drawer opener.
        opener = page.locator(  # type: ignore[union-attr]
            'a[hx-get^="/projects/1/frames/"][hx-get$="/drawer"]'
        ).first
        opener.click()

        page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]

        title = page.locator("#drawer-title").inner_text(  # type: ignore[union-attr]
            timeout=_TIMEOUT
        )
        assert title.startswith("Frame #")

        # Body carries the scroll-lock state.
        assert page.evaluate(  # type: ignore[union-attr]
            "document.body.classList.contains('drawer-open')"
        )

    def test_navigation_control_reswaps_drawer(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """A prev/next control re-swaps the drawer body to a different frame."""
        page, base = logged_in_data_page  # type: ignore[misc]

        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )

        opener = page.locator(  # type: ignore[union-attr]
            'a[hx-get^="/projects/1/frames/"][hx-get$="/drawer"]'
        ).first
        opener.click()
        page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]

        title_before = page.locator("#drawer-title").inner_text(  # type: ignore[union-attr]
            timeout=_TIMEOUT
        )

        # Click the "Older" control inside the drawer (the newest tile is first,
        # so an older neighbour always exists).
        older = page.locator(  # type: ignore[union-attr]
            "#drawer-main .drawer-body a[rel='next']"
        ).first
        older.click()

        # The drawer stays open and the title changes to a different frame.
        page.wait_for_function(  # type: ignore[union-attr]
            "(t) => { var el = document.querySelector('#drawer-title'); "
            "return el && el.textContent.trim() !== t && "
            "el.textContent.trim().indexOf('Frame #') === 0; }",
            arg=title_before,
            timeout=_TIMEOUT,
        )
        assert page.evaluate(_DRAWER_OPEN_JS)  # type: ignore[union-attr]


class TestNoJsFallback:
    """A direct GET of the drawer route renders a full standalone page."""

    def test_direct_get_renders_full_page(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        """GET /projects/1/frames/<id>/drawer without HTMX returns the full page
        (frame_detail.html extending base.html), which includes the app nav."""
        page, base = logged_in_data_page  # type: ignore[misc]

        # Resolve a real frame id by scraping a tile's opener href first.
        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        frame_id = page.evaluate(  # type: ignore[union-attr]
            "() => { var a = document.querySelector("
            '\'a[hx-get^="/projects/1/frames/"][hx-get$="/drawer"]\'); '
            "var m = a && a.getAttribute('hx-get').match("
            "/frames\\/(\\d+)\\/drawer/); return m ? m[1] : null; }"
        )
        assert frame_id is not None

        page.goto(  # type: ignore[union-attr]
            f"{base}/projects/1/frames/{frame_id}/drawer",
            wait_until="domcontentloaded",
        )

        # Full page includes the app nav shell.
        page.wait_for_selector(".app-nav", timeout=_TIMEOUT)  # type: ignore[union-attr]
        assert page.locator(".app-nav").count() >= 1  # type: ignore[union-attr]
        # The full-page detail renders the frame heading.
        assert page.locator("text=Frame #").count() >= 1  # type: ignore[union-attr]


class TestRenderExclusion:
    """The frame drawer surfaces the render-inclusion control: an included frame
    shows a 'Render: Included' row and an 'Exclude from render' action.

    This is a render-only browser assertion that the new drawer controls exist;
    the exclude/include mutation behaviour (state flip, badge, audit, role-gating,
    CSRF) is covered deterministically by the web-layer tests. A browser
    round-trip of the mutation is intentionally not exercised here: drawer
    mutation forms post asynchronously via HTMX and the test server's SQLite can
    fail CSRF closed under the page's concurrent traffic, which is unrelated to
    this feature.
    """

    def test_drawer_shows_render_inclusion_control(
        self, logged_in_data_page: tuple[object, str]
    ) -> None:
        page, base = logged_in_data_page  # type: ignore[misc]

        page.goto(  # type: ignore[union-attr]
            f"{base}/frames?project_id=1", wait_until="domcontentloaded"
        )
        page.locator(  # type: ignore[union-attr]
            'a[hx-get^="/projects/1/frames/"][hx-get$="/drawer"]'
        ).first.click()
        page.wait_for_function(_DRAWER_OPEN_JS, timeout=_TIMEOUT)  # type: ignore[union-attr]

        body = page.locator("#drawer-main .drawer-body")  # type: ignore[union-attr]
        # The new "Render" metadata row is present, reading Included by default.
        body.locator("dt:has-text('Render')").wait_for(timeout=_TIMEOUT)
        assert body.locator("dd:has-text('Included')").count() >= 1
        # The operator-only Exclude control is offered (admin data page is an
        # operator), with no badge while the frame is still included.
        assert body.locator("button:has-text('Exclude from render')").count() == 1
        assert body.locator(".badge-excluded").count() == 0
