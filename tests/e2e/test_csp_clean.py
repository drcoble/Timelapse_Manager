"""Every page loads cleanly under the CSP and the global htmx config (#70).

The safety net for global frontend changes. WP-70-B is the first change to
global htmx behaviour (``allowEval:false``, ``selfRequestsOnly``,
``historyCacheSize:0``, the 30 s timeout); later CSP work tightens the policy
itself. Signed in as the admin, every top-level screen is visited and must
produce:

* no ``securitypolicyviolation`` event;
* no uncaught page error;
* no console error, apart from the 404s for the seeded frames' image files
  (the seed rows point at JPEGs that do not exist on disk);
* nothing in ``localStorage`` except the ``theme`` preference (htmx keeps no
  history snapshots).

Chromium always runs. Firefox and WebKit run when their Playwright browsers are
installed and skip otherwise.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Generator

import pytest

from tests.e2e.conftest import SecurityEvents, login

pytestmark = pytest.mark.ui

# The top-level screens. The seeded project is id 1.
_PAGES = (
    "/",
    "/projects",
    "/projects/1",
    "/frames?project_id=1",
    "/frames",
    "/events",
    "/renders",
    "/cameras",
    "/users",
    "/settings",
    "/events/audit",
    "/account/preferences",
    "/about",
)

# Frame image routes (full image and thumbnail). The seed frames have no files,
# so these 404 by design and are the only tolerated console errors.
_FRAME_IMAGE_URL = re.compile(r"/frames/\d+/(image|thumbnail)\b")


@pytest.fixture(params=["chromium", "firefox", "webkit"])
def browser_page(request: pytest.FixtureRequest) -> Generator[object, None, None]:
    """A fresh page in each Playwright engine; skip an engine that is missing."""
    sync_api = pytest.importorskip("playwright.sync_api")
    pw = sync_api.sync_playwright().start()
    try:
        try:
            browser = getattr(pw, request.param).launch(headless=True)
        except sync_api.Error as exc:
            pytest.skip(f"{request.param} browser not installed: {exc}")
        context = browser.new_context(viewport={"width": 1280, "height": 800})
        try:
            yield context.new_page()
        finally:
            context.close()
            browser.close()
    finally:
        pw.stop()


def _unexpected_console_errors(events: SecurityEvents) -> list[tuple[str, str]]:
    return [
        (text, url)
        for text, url in events.console_errors
        if not (_FRAME_IMAGE_URL.search(url) and "404" in text)
    ]


def test_all_pages_load_without_csp_or_console_errors(
    browser_page: object,
    live_server_with_data: str,
    security_events: Callable[[object], SecurityEvents],
) -> None:
    page = browser_page
    base = live_server_with_data
    events = security_events(page)
    login(page, base)
    events.clear()

    problems: dict[str, dict[str, object]] = {}
    for path in _PAGES:
        # Leaving a page aborts its in-flight htmx polls, and Firefox and WebKit
        # report those as htmx:sendAbort / sendError console errors from the
        # page being left. Step through a blank page, so they are cleared
        # before the page under test loads rather than charged to it.
        page.goto("about:blank")  # type: ignore[attr-defined]
        events.clear()
        response = page.goto(f"{base}{path}", wait_until="networkidle")  # type: ignore[attr-defined]
        status = response.status if response is not None else None
        found: dict[str, object] = {}
        if status != 200:
            found["status"] = status
        if events.csp_violations:
            found["csp"] = list(events.csp_violations)
        if events.page_errors:
            found["pageerror"] = list(events.page_errors)
        console = _unexpected_console_errors(events)
        if console:
            found["console"] = console
        if found:
            problems[path] = found

    assert problems == {}

    keys = page.evaluate("() => Object.keys(localStorage)")  # type: ignore[attr-defined]
    assert set(keys) <= {"theme"}, keys


def test_project_forms_load_cleanly_under_the_csp(
    logged_in_data_page: tuple[object, str],
    security_events: Callable[[object], SecurityEvents],
) -> None:
    """The drawer and tab forms (WP-70-M1) load and work with no violation.

    Opens the New Project drawer, the project Settings tab and the milestone
    form, and flips one converted control in each. Whatever is blocked or
    thrown shows up here, not as a dead control in front of the user.
    """
    page, base = logged_in_data_page
    events = security_events(page)
    page.goto(f"{base}/", wait_until="networkidle")  # type: ignore[attr-defined]
    events.clear()

    # New Project drawer: schedule preset, then capture mode (solar hides the
    # schedule fields).
    page.locator('a[hx-get="/drawers/new-project"]').first.click()  # type: ignore[attr-defined]
    page.wait_for_selector("#drawer-main #capture_interval_value")  # type: ignore[attr-defined]
    page.check("#drawer-main input[name='capture_schedule_preset'][value='custom']")  # type: ignore[attr-defined]
    page.check("#drawer-main input[name='capture_mode'][value='solar']")  # type: ignore[attr-defined]

    # Settings tab and milestone form on the project page.
    page.goto(f"{base}/projects/1", wait_until="networkidle")  # type: ignore[attr-defined]
    page.locator('[data-tab-target="#tab-settings"]').click()  # type: ignore[attr-defined]
    page.wait_for_selector('#tab-settings form[action="/projects/1/settings"]')  # type: ignore[attr-defined]
    page.check("#tab-settings input[name='capture_schedule_preset'][value='sun']")  # type: ignore[attr-defined]
    page.click("#tab-settings button[data-action='exact-add']")  # type: ignore[attr-defined]
    page.locator('[data-tab-target="#tab-milestones"]').click()  # type: ignore[attr-defined]
    page.locator('button[hx-get="/projects/1/milestones/new"]').click()  # type: ignore[attr-defined]
    page.wait_for_selector("#ms-new-card")  # type: ignore[attr-defined]
    page.check("#ms-new-type-time")  # type: ignore[attr-defined]
    page.wait_for_load_state("networkidle")  # type: ignore[attr-defined]

    assert events.csp_violations == []
    assert events.page_errors == []
    assert _unexpected_console_errors(events) == []


def test_htmx_config_is_applied(logged_in_page: object) -> None:
    page = logged_in_page
    # login() returns once .app-shell exists, which can be before htmx has read
    # its <meta name="htmx-config"> at DOMContentLoaded: wait for the load event
    # and for htmx, so the read is deterministic (#189).
    page.wait_for_load_state("load")  # type: ignore[attr-defined]
    page.wait_for_function("() => typeof window.htmx !== 'undefined'")  # type: ignore[attr-defined]
    config = page.evaluate(  # type: ignore[attr-defined]
        "() => ({timeout: htmx.config.timeout, allowEval: htmx.config.allowEval,"
        " selfRequestsOnly: htmx.config.selfRequestsOnly,"
        " historyCacheSize: htmx.config.historyCacheSize,"
        " includeIndicatorStyles: htmx.config.includeIndicatorStyles,"
        " allowScriptTags: htmx.config.allowScriptTags})"
    )
    assert config == {
        "timeout": 30000,
        "allowEval": False,
        "selfRequestsOnly": True,
        "historyCacheSize": 0,
        "includeIndicatorStyles": False,
        "allowScriptTags": False,
    }
    assert page.evaluate("() => typeof window.tlmToast") == "function"  # type: ignore[attr-defined]


def test_recorder_sees_a_violation_and_a_page_error(
    logged_in_page: object,
    security_events: Callable[[object], SecurityEvents],
) -> None:
    """The safety net itself works: a blocked image and a throw are both caught.

    (Playwright's ``evaluate`` is exempt from the page CSP, so a blocked
    ``eval`` cannot serve as the probe; a cross-origin image is a real
    ``img-src`` violation. The image never loads: the CSP blocks it first.)
    """
    page = logged_in_page
    events = security_events(page)
    page.reload(wait_until="networkidle")  # type: ignore[attr-defined]
    page.evaluate(  # type: ignore[attr-defined]
        "() => { const i = document.createElement('img');"
        " i.src = 'https://csp-probe.invalid/x.png'; document.body.appendChild(i); }"
    )
    page.evaluate("() => setTimeout(() => { throw new Error('boom'); })")  # type: ignore[attr-defined]
    for _ in range(50):
        if events.csp_violations and events.page_errors:
            break
        page.wait_for_timeout(100)  # type: ignore[attr-defined]
    assert [v["directive"] for v in events.csp_violations] == ["img-src"]
    assert any("boom" in e for e in events.page_errors)
