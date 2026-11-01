"""CSRF rotation in a real browser (#70 F-R3, WP-70-G; #165).

Two tabs share one cookie jar. Tab B signs out and in again, so the browser
now sends the new session's cookie while tab A still holds the old session's
token. Once tab A's alerts poll runs (here: the tab coming back into view),
``/alerts/summary`` answers with a fresh token and ``csrf.js`` writes it into
the meta tag and every form:

* a dashboard card form -- which the 204 grid poll never re-renders (#165) --
  submits successfully, even when its own hidden field is stale (the submit
  handler reads the meta);
* an htmx action (a frame's Remove) succeeds.

Without the poll, the htmx action gets the CSRF 403 and the Reload toast.

The card action is **Start**, not Pause: the e2e server runs with
``capture.autostart=False``, so no card ever reads ``running`` and Pause is
never rendered; the project reads ``stopped`` and its card carries Start. That
is the same plain card form and token path. Start on an already-active project
leaves it active, and waking a supervisor that was never started is a no-op
(``CaptureSupervisor.notify_reconcile``), so no capture launches.

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from tests.e2e.conftest import _boot_app, _seed_demo_data, login, stop_app

pytestmark = pytest.mark.ui

_TIMEOUT = 15_000
_META_JS = "() => document.querySelector('meta[name=\"csrf-token\"]').content"
_ALERT_TOAST = "#toast-region .toast[role=alert]"
_CARD_START = '#project-card-1 form[action="/projects/1/start"]'


@pytest.fixture()
def server(
    tmp_path: Path, browser_context_factory: Callable[..., Any]
) -> Generator[tuple[Any, str], None, None]:
    """A signed-in tab on a server with the demo camera, project 1 and frames.

    The tab lives in its own context (the default page's context cannot open a
    second page), so a second tab opened from it shares its cookie jar.
    """
    proc, base_url = _boot_app(tmp_path, extra_seed=_seed_demo_data)
    try:
        page = browser_context_factory(copy_cookies=False).new_page()
        login(page, base_url)
        yield page, base_url
    finally:
        stop_app(proc)


def _path(url: str) -> str:
    return urlsplit(url).path


def _sign_in_again_in_another_tab(page: Any, base_url: str) -> None:
    """Sign out and back in from a second tab sharing ``page``'s cookies."""
    other = page.context.new_page()
    try:
        other.goto(f"{base_url}/about", wait_until="domcontentloaded")
        with other.expect_navigation():
            other.locator('form[action="/logout"] button[type=submit]').evaluate(
                "b => b.form.requestSubmit(b)"
            )
        login(other, base_url)
    finally:
        other.close()
    page.bring_to_front()


def _record_alert_polls(page: Any) -> list[Any]:
    """Collect every ``/alerts/summary`` request ``page`` sends from now on."""
    seen: list[Any] = []
    page.on(
        "request",
        lambda r: seen.append(r) if _path(r.url) == "/alerts/summary" else None,
    )
    return seen


def _rotate_via_poll(page: Any, old_token: str, polls: list[Any]) -> str:
    """Run tab A's alerts poll as a tab switch-back does; return the new token.

    Coming back to the front may already have fired the poll (poll.js sends
    ``tlm:visible`` when a hidden tab becomes visible); dispatching it again is
    harmless, so the test does not depend on how headless reports visibility.
    """
    page.evaluate("() => document.dispatchEvent(new Event('tlm:visible'))")
    page.wait_for_function(
        "old => document.querySelector('meta[name=\"csrf-token\"]').content !== old",
        arg=old_token,
        timeout=_TIMEOUT,
    )
    # The poll carried the page's (old) token: that is how the server knew.
    assert old_token in [r.headers.get("x-csrf-token") for r in polls]
    new_token: str = page.evaluate(_META_JS)
    return new_token


def test_card_form_on_an_open_dashboard_works_after_rotation(
    server: tuple[Any, str],
) -> None:
    page, base_url = server
    page.goto(f"{base_url}/", wait_until="networkidle")
    old_token = page.evaluate(_META_JS)
    page.locator(_CARD_START).wait_for(state="attached", timeout=_TIMEOUT)

    polls = _record_alert_polls(page)
    _sign_in_again_in_another_tab(page, base_url)
    _rotate_via_poll(page, old_token, polls)

    # tlm:csrf rewrote every form field on the page, the card's included. (Not
    # compared with the meta: a later poll may write another mask of the same
    # secret, which is equally valid.)
    field = page.locator(f'{_CARD_START} input[name="csrf_token"]')
    assert field.input_value() != old_token
    # And a field that is stale anyway is refreshed from the meta on submit.
    field.evaluate("el => { el.value = 'stale-token'; }")

    with page.expect_response(
        lambda r: _path(r.url) == "/projects/1/start" and r.request.method == "POST"
    ) as info:
        page.locator(f"{_CARD_START} button[type=submit]").click()
    assert info.value.status == 303
    page.wait_for_url(f"{base_url}/projects/1", timeout=_TIMEOUT)
    assert "CSRF validation failed" not in page.content()


def test_htmx_action_works_after_rotation(server: tuple[Any, str]) -> None:
    page, base_url = server
    page.goto(f"{base_url}/frames?project_id=1", wait_until="networkidle")
    old_token = page.evaluate(_META_JS)

    polls = _record_alert_polls(page)
    _sign_in_again_in_another_tab(page, base_url)
    _rotate_via_poll(page, old_token, polls)

    remove = page.locator('button[hx-post*="/soft-delete"]').first
    with page.expect_response(lambda r: "/soft-delete" in _path(r.url)) as info:
        remove.click(force=True)
    assert info.value.status == 200
    # Not compared with one mask: a later poll may write another, equally valid.
    assert info.value.request.headers.get("x-csrf-token") != old_token
    assert page.locator(_ALERT_TOAST).count() == 0


def test_without_the_poll_the_htmx_action_offers_reload(
    server: tuple[Any, str],
) -> None:
    page, base_url = server
    page.goto(f"{base_url}/frames?project_id=1", wait_until="networkidle")
    # Hold the alerts poll so nothing refreshes the token before the click.
    page.evaluate(
        "() => document.getElementById('alerts-area')"
        ".setAttribute('data-poll-paused', '')"
    )

    _sign_in_again_in_another_tab(page, base_url)

    remove = page.locator('button[hx-post*="/soft-delete"]').first
    with page.expect_response(lambda r: "/soft-delete" in _path(r.url)) as info:
        remove.click(force=True)
    assert info.value.status == 403
    toast = page.locator(_ALERT_TOAST, has_text="session changed").first
    toast.wait_for(timeout=_TIMEOUT)
    assert toast.locator(".toast-action", has_text="Reload").count() == 1
