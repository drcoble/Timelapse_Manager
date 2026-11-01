"""The polling policy in a real browser (#70 F-S1, F-P3; WP-70-E1).

* An idle page signs out after the idle timeout even though its polls keep
  running; a page whose user keeps typing stays signed in.
* Polls stop while the tab is hidden and refresh once when it comes back.
* A hung server never piles up fetch polls, and a 401 on a fetch poll sends the
  browser to the login page with the current page as ``next``.
* Controls inside a polled container keep the double-submit guard (WP-70-D):
  only the poller itself opts out.

The two idle tests run on real time with a server whose idle timeout is 8 s
(touch interval 1 s). They do not use ``page.clock``: a fake clock also fakes
``Date.now()`` in the page, which would make ``X-TLM-Idle`` jump by the
fast-forwarded time. They fire the poll with the ``tlm:visible`` event the
pollers already listen for.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

import re
from collections.abc import Generator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from tests.e2e.conftest import _boot_app, login, make_frame_seeder, stop_app

pytestmark = pytest.mark.ui

_IDLE_TIMEOUT_S = 8
_FIRE_POLL = "document.dispatchEvent(new Event('tlm:visible'))"
_SET_HIDDEN = """(hidden) => {
  Object.defineProperty(document, 'hidden', {configurable: true, get: () => hidden});
  Object.defineProperty(document, 'visibilityState', {
    configurable: true, get: () => (hidden ? 'hidden' : 'visible')
  });
  document.dispatchEvent(new Event('visibilitychange'));
}"""
_HTMX_POLLS = ("/alerts/summary", "/partials/projects")


@pytest.fixture()
def short_idle(
    tmp_path: Path, _chromium_page: Any
) -> Generator[tuple[Any, str], None, None]:
    """A signed-in page on a server whose idle timeout is 8 s."""
    proc, base_url = _boot_app(
        tmp_path, server_args=["--idle-timeout-seconds", str(_IDLE_TIMEOUT_S)]
    )
    try:
        login(_chromium_page, base_url)
        yield _chromium_page, base_url
    finally:
        stop_app(proc)


@pytest.fixture()
def with_project(
    tmp_path: Path, _chromium_page: Any
) -> Generator[tuple[Any, str], None, None]:
    """A signed-in page on a server with project 1 and three frames."""
    proc, base_url = _boot_app(
        tmp_path, extra_seed=make_frame_seeder(3, project_name="Polling")
    )
    try:
        login(_chromium_page, base_url)
        yield _chromium_page, base_url
    finally:
        stop_app(proc)


def _path_is(request: Any, path: str) -> bool:
    """Whether ``request`` is for exactly ``path`` (not a sub-path like a ribbon)."""
    return urlsplit(request.url).path == path


def _open_dashboard(page: Any, base_url: str) -> None:
    """Load ``/`` and wait for its first alerts poll (the ``load`` trigger)."""
    with page.expect_response(lambda r: "/alerts/summary" in r.url):
        page.goto(f"{base_url}/")


# ---- 1, 2: idle sign-out vs an active user ---------------------------------


def test_idle_page_signs_out_after_the_idle_timeout(
    short_idle: tuple[Any, str],
) -> None:
    page, base_url = short_idle
    _open_dashboard(page, base_url)
    page.wait_for_timeout((_IDLE_TIMEOUT_S + 1) * 1000)
    page.evaluate(_FIRE_POLL)
    page.wait_for_url(re.compile(r"/login\?next=%2F$"), timeout=15_000)


def test_active_user_stays_signed_in(short_idle: tuple[Any, str]) -> None:
    page, base_url = short_idle
    _open_dashboard(page, base_url)
    for _ in range(5):  # 10 s in all: longer than the 8 s idle timeout
        page.wait_for_timeout(2000)
        page.keyboard.press("Shift")
        with page.expect_response(lambda r: "/alerts/summary" in r.url) as info:
            page.evaluate(_FIRE_POLL)
        assert info.value.status == 200
        assert info.value.request.headers.get("x-tlm-idle") == "0"
    assert page.url == f"{base_url}/"
    assert page.locator(".app-shell").count() == 1


# ---- 3: hidden tab --------------------------------------------------------


def test_hidden_tab_does_not_poll_and_refreshes_once_on_return(
    with_project: tuple[Any, str],
) -> None:
    page, base_url = with_project
    page.clock.install()
    seen: list[Any] = []
    page.on("request", lambda req: seen.append(req))
    _open_dashboard(page, base_url)

    # The pollers are marked passive; an ordinary partial load is not.
    alerts = next(r for r in seen if "/alerts/summary" in r.url)
    assert alerts.headers.get("x-tlm-passive") == "1"
    assert re.fullmatch(r"[0-9]+", alerts.headers.get("x-tlm-idle", ""))
    status = [r for r in seen if "/partials/status" in r.url]
    assert status and "x-tlm-passive" not in status[0].headers

    page.evaluate(_SET_HIDDEN, True)
    seen.clear()
    page.clock.run_for(120_000)
    page.wait_for_timeout(500)
    assert [r.url for r in seen if any(_path_is(r, p) for p in _HTMX_POLLS)] == []

    with (
        page.expect_response(lambda r: _path_is(r, "/alerts/summary")),
        page.expect_response(lambda r: _path_is(r, "/partials/projects")),
    ):
        page.evaluate(_SET_HIDDEN, False)
    page.wait_for_timeout(500)
    for path in _HTMX_POLLS:
        assert len([r for r in seen if _path_is(r, path)]) == 1, path


# ---- 4, 5: fetch pollers --------------------------------------------------

# Counts the frames poll's in-flight fetches in the page, so the check does
# not depend on how Chromium reports an aborted, intercepted request.
_COUNT_FETCHES = """
(() => {
  const real = window.fetch;
  window.__polls = {started: 0, inflight: 0, maxInflight: 0};
  window.fetch = function (url, opts) {
    const counted = String(url).indexOf('/frames/since') !== -1;
    if (!counted) return real.apply(this, arguments);
    const s = window.__polls;
    s.started += 1;
    s.inflight += 1;
    s.maxInflight = Math.max(s.maxInflight, s.inflight);
    return real.apply(this, arguments).finally(() => { s.inflight -= 1; });
  };
})();
"""


def test_hung_server_never_piles_up_frame_polls(with_project: tuple[Any, str]) -> None:
    page, base_url = with_project
    held: list[Any] = []
    page.route("**/frames/since**", lambda route: held.append(route))
    page.add_init_script(_COUNT_FETCHES)
    page.clock.install()
    page.goto(f"{base_url}/frames?project_id=1")
    page.wait_for_selector("#frame-new-pill", state="attached")

    page.clock.run_for(200_000)
    page.wait_for_timeout(500)
    polls = page.evaluate("window.__polls")
    # 30 s: first poll; 45 s: aborted; backoff 60 s -> 105 s; aborted at 120 s;
    # backoff 120 s -> 240 s, past the window. Never two at once.
    assert polls["maxInflight"] == 1
    assert 1 <= polls["started"] <= 2


def test_fetch_poll_401_goes_to_login(with_project: tuple[Any, str]) -> None:
    page, base_url = with_project
    page.route(
        "**/frames/since**",
        lambda route: route.fulfill(
            status=401,
            content_type="application/json",
            body='{"detail": "Authentication required."}',
        ),
    )
    page.goto(f"{base_url}/frames?project_id=1")
    page.wait_for_selector("#frame-new-pill", state="attached")
    # Back-to-visible runs every fetch poller once at once.
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_url(re.compile(r"/login\?next=%2Fframes%3Fproject_id%3D1$"))


# ---- forms.js still guards controls inside a polled container -------------


def test_card_form_inside_the_polled_grid_submits_once(
    with_project: tuple[Any, str],
) -> None:
    page, base_url = with_project
    _open_dashboard(page, base_url)
    posts: list[Any] = []

    def hold(route: Any) -> None:
        if route.request.method == "POST":
            posts.append(route)  # never answered: the first submit stays in flight
        else:
            route.continue_()

    page.route("**/projects/1/*", hold)
    button = page.locator(
        "#projects-grid form[method='post'] button[type='submit']"
    ).first
    # Chromium drops the second submit of a fast double click on its own, so
    # click twice 150 ms apart with raw mouse events (as test_double_submit.py
    # does): without the guard that sends a second POST.
    button.scroll_into_view_if_needed()
    box = button.bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    page.mouse.click(x, y)
    page.wait_for_timeout(150)
    page.mouse.click(x, y)
    page.wait_for_timeout(1000)
    assert len(posts) == 1, "the second click sent a second POST"


# ---- htmx-errors.js still toasts a failed action inside a polled container --

_INJECT_ACTION = """() => {
  const area = document.getElementById('alerts-area');
  const b = document.createElement('button');
  b.type = 'button';
  b.id = 'e2e-inner-action';
  b.textContent = 'Clear';
  b.setAttribute('hx-post', '/alerts/e2e-fail/clear');
  b.setAttribute('hx-swap', 'none');
  area.appendChild(b);
  htmx.process(b);
}"""


def test_failed_action_inside_a_polled_container_still_toasts(
    with_project: tuple[Any, str],
) -> None:
    page, base_url = with_project
    _open_dashboard(page, base_url)
    page.route(
        "**/alerts/e2e-fail/clear",
        lambda route: route.fulfill(status=500, content_type="text/plain", body="boom"),
    )
    # Pause the alerts poll and let its first swap settle, so no later swap
    # replaces the injected button while its request is in flight.
    page.evaluate(
        "document.getElementById('alerts-area').setAttribute('data-poll-paused', '')"
    )
    page.wait_for_load_state("networkidle")
    page.evaluate(_INJECT_ACTION)
    page.evaluate("document.getElementById('e2e-inner-action').click()")
    page.wait_for_selector("#toast-region .toast.error", timeout=10_000)
