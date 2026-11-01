"""The alerts panel by keyboard, pointer and touch (#70 F-R6; WP-70-E2).

The toggle and panel are static; alerts.js owns the open state. A background
poll can neither close an open panel nor take the toggle's focus, Escape
closes and returns focus, a click outside closes, a dismiss keeps focus in the
panel, and a tap opens it on a touch device (no hover dependency).

Requires the ``ui`` marker; auto-skips when Playwright browsers are absent.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

from tests.e2e.conftest import _boot_app, login, stop_app

pytestmark = pytest.mark.ui

_TOGGLE = "#alerts-toggle"
_PANEL = "#alerts-panel"
_ACTIVE_ID = "document.activeElement && document.activeElement.id"


def _seed_alerts(db_url: str, data_dir: Path) -> None:
    from timelapse_manager.db.engine import create_db_engine
    from timelapse_manager.db.session import create_session_factory, session_scope
    from timelapse_manager.monitoring import log_event

    engine = create_db_engine(db_url)
    try:
        with session_scope(create_session_factory(engine)) as db:
            for n in (1, 2):
                log_event(
                    db,
                    scope="system",
                    scope_id=None,
                    level="warning",
                    message=f"E2E alert {n}",
                )
    finally:
        engine.dispose()


@pytest.fixture()
def alerts_page(
    tmp_path: Path, _chromium_page: Any
) -> Generator[tuple[Any, str], None, None]:
    """A signed-in admin page on a server with two active alerts."""
    proc, base_url = _boot_app(tmp_path, extra_seed=_seed_alerts)
    try:
        login(_chromium_page, base_url)
        yield _chromium_page, base_url
    finally:
        stop_app(proc)


def _is_summary(response: Any) -> bool:
    return urlsplit(response.url).path == "/alerts/summary"


def _open_dashboard(page: Any, base_url: str) -> None:
    with page.expect_response(_is_summary):
        page.goto(f"{base_url}/")
    page.wait_for_selector("#alerts-badge.alerts-badge", state="attached")


def test_keyboard_open_survives_a_poll_and_escape_returns_focus(
    alerts_page: tuple[Any, str],
) -> None:
    page, base_url = alerts_page
    page.clock.install()
    _open_dashboard(page, base_url)
    toggle = page.locator(_TOGGLE)
    panel = page.locator(_PANEL)
    assert toggle.get_attribute("aria-expanded") == "false"
    assert panel.is_hidden()
    assert "Active alerts: 2" in (
        page.locator("#alerts-count-label").text_content() or ""
    )

    toggle.focus()
    page.keyboard.press("Enter")
    assert toggle.get_attribute("aria-expanded") == "true"
    assert panel.is_visible()
    assert "E2E alert 1" in (panel.text_content() or "")

    summaries: list[Any] = []
    page.on("request", lambda r: summaries.append(r) if _is_summary(r) else None)
    page.clock.run_for(30_000)
    page.wait_for_timeout(500)
    assert summaries == []  # paused while open
    assert page.evaluate(_ACTIVE_ID) == "alerts-toggle"
    assert panel.is_visible()

    with page.expect_response(_is_summary):  # one refresh on close
        page.keyboard.press("Escape")
    assert panel.is_hidden()
    assert toggle.get_attribute("aria-expanded") == "false"
    assert page.evaluate(_ACTIVE_ID) == "alerts-toggle"


def test_hover_does_not_open_and_click_outside_closes(
    alerts_page: tuple[Any, str],
) -> None:
    page, base_url = alerts_page
    _open_dashboard(page, base_url)
    page.hover(_TOGGLE)
    assert page.locator(_PANEL).is_hidden()
    page.click(_TOGGLE)
    assert page.locator(_PANEL).is_visible()
    page.click(_PANEL + " .alerts-panel-title")  # inside: stays open
    assert page.locator(_PANEL).is_visible()
    url = page.url
    page.click(".page-title h1")  # outside, and inert
    assert page.url == url
    assert page.locator(_PANEL).is_hidden()
    assert page.locator(_TOGGLE).get_attribute("aria-expanded") == "false"


def test_dismiss_keeps_the_panel_open_with_focus_inside(
    alerts_page: tuple[Any, str],
) -> None:
    page, base_url = alerts_page
    _open_dashboard(page, base_url)
    page.click(_TOGGLE)
    page.locator(".alerts-dismiss").first.focus()
    with page.expect_response(lambda r: urlsplit(r.url).path.endswith("/clear")):
        page.keyboard.press("Enter")
    page.wait_for_function(
        "() => document.getElementById('alerts-count-label').textContent"
        ".includes('Active alerts: 1')"
    )
    assert page.locator(_PANEL).is_visible()
    assert page.locator(".alerts-dismiss").count() == 1
    assert page.locator("#alerts-badge").text_content() == "1"
    page.wait_for_function(f"() => ({_ACTIVE_ID}) === 'alerts-panel'")


def test_tap_opens_on_a_touch_device(
    alerts_page: tuple[Any, str],
    browser_context_factory: Callable[..., Any],
) -> None:
    _, base_url = alerts_page
    context = browser_context_factory(
        viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True
    )
    page = context.new_page()
    _open_dashboard(page, base_url)
    page.tap(_TOGGLE)
    assert page.locator(_PANEL).is_visible()
    assert page.locator(_TOGGLE).get_attribute("aria-expanded") == "true"
    page.tap(_TOGGLE)
    assert page.locator(_PANEL).is_hidden()
