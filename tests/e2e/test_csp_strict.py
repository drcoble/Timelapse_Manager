"""Strict CSP end to end (#70 F-S3, #67-S17; WP-70-M3).

``script-src 'self'`` with no ``'unsafe-inline'``: the bootstrap scripts are
external files, htmx refuses script tags in swapped fragments, and the theme is
still applied before the first paint.
"""

from __future__ import annotations

import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
from alembic import command as alembic_command

from tests.e2e.conftest import (
    SecurityEvents,
    _alembic_cfg_for,
    _free_port,
    stop_app,
)

pytestmark = pytest.mark.ui

_DCL_PROBE = (
    "document.addEventListener('DOMContentLoaded', () => {"
    " window.__themeAtDcl = document.documentElement.dataset.theme || null; });"
)


@pytest.fixture()
def fresh_server(tmp_path: Path) -> Generator[str, None, None]:
    """The app against an empty database (no admin yet), so /first-run renders."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    db_url = f"sqlite:///{tmp_path / 'fresh.db'}"
    alembic_command.upgrade(_alembic_cfg_for(db_url), "head")
    port = _free_port()
    log = (tmp_path / "server.log").open("wb")
    proc = subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).parent / "_serve.py"),
            "--db-url",
            db_url,
            "--data-dir",
            str(data_dir),
            "--port",
            str(port),
        ],
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    log.close()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 45.0
    ready = False
    while time.monotonic() < deadline and not ready:
        try:
            with urllib.request.urlopen(f"{base}/first-run", timeout=2.0) as resp:
                ready = resp.status == 200
        except Exception:  # noqa: BLE001
            time.sleep(0.2)
    if not ready:
        stop_app(proc)
        raise RuntimeError("fresh e2e server did not become ready")
    try:
        yield base
    finally:
        stop_app(proc)


def _goto_clean(page: object, events: SecurityEvents, url: str, *, status: int) -> None:
    events.clear()
    response = page.goto(url, wait_until="networkidle")  # type: ignore[attr-defined]
    assert response is not None
    assert response.status == status
    assert events.csp_violations == []
    assert events.page_errors == []


def test_csp_header_is_strict(live_server: str) -> None:
    for path in ("/login", "/nope-not-a-route"):  # HTML page, then a 404
        try:
            resp = urllib.request.urlopen(f"{live_server}{path}", timeout=5)
            headers = resp.headers
        except urllib.error.HTTPError as exc:
            headers = exc.headers
        csp = headers["Content-Security-Policy"]
        assert "script-src 'self';" in csp
        assert "'unsafe-eval'" not in csp
        assert "script-src 'self' 'unsafe-inline'" not in csp


def test_pre_auth_and_error_pages_are_clean(
    browser_context_factory: Callable[..., object],
    live_server: str,
    security_events: Callable[[object], SecurityEvents],
) -> None:
    page = browser_context_factory().new_page()  # type: ignore[attr-defined]
    events = security_events(page)
    _goto_clean(page, events, f"{live_server}/login", status=200)
    # The sign-in theme toggle (login-theme.js) still cycles and remembers.
    before = page.evaluate("() => document.documentElement.dataset.theme || null")
    page.click("#login-theme-toggle")
    after = page.evaluate("() => document.documentElement.dataset.theme || null")
    assert after != before
    assert page.evaluate("() => localStorage.getItem('theme')") == (after or "system")


def test_first_run_page_is_clean(
    fresh_server: str,
    browser_context_factory: Callable[..., object],
    security_events: Callable[[object], SecurityEvents],
) -> None:
    page = browser_context_factory().new_page()  # type: ignore[attr-defined]
    events = security_events(page)
    _goto_clean(page, events, f"{fresh_server}/first-run", status=200)
    assert page.locator("form").count() >= 1


def test_htmx_refuses_script_tags_in_swapped_fragments(
    logged_in_page: object,
    security_events: Callable[[object], SecurityEvents],
) -> None:
    page = logged_in_page
    events = security_events(page)
    page.reload(wait_until="networkidle")  # type: ignore[attr-defined]
    assert page.evaluate("() => htmx.config.allowScriptTags") is False  # type: ignore[attr-defined]
    page.route(  # type: ignore[attr-defined]
        "**/__probe",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html",
            body="<p id='probe-ok'>ok</p><script>window.__pwned=1</script>",
        ),
    )
    page.evaluate(  # type: ignore[attr-defined]
        "() => { const b = document.createElement('button'); b.id = 'probe';"
        " b.setAttribute('hx-get', '/__probe');"
        " b.setAttribute('hx-target', '#probe-out');"
        " const o = document.createElement('div'); o.id = 'probe-out';"
        " document.body.append(b, o); htmx.process(b); b.click(); }"
    )
    page.wait_for_selector("#probe-ok")  # type: ignore[attr-defined]
    assert page.evaluate("() => window.__pwned === undefined") is True  # type: ignore[attr-defined]
    assert events.csp_violations == []


def test_theme_is_set_before_first_paint(
    logged_in_page: object,
) -> None:
    page = logged_in_page
    page.evaluate(  # type: ignore[attr-defined]
        "() => fetch('/account/theme', {method: 'POST', headers: {"
        "'Content-Type': 'application/x-www-form-urlencoded',"
        " 'X-CSRF-Token': document.querySelector('meta[name=csrf-token]').content},"
        " body: 'theme=dark'})"
    )
    page.evaluate("() => localStorage.clear()")  # type: ignore[attr-defined]
    page.add_init_script(_DCL_PROBE)  # type: ignore[attr-defined]
    page.reload(wait_until="load")  # type: ignore[attr-defined]
    assert page.evaluate("() => window.__themeAtDcl") == "dark"  # type: ignore[attr-defined]
    # Anonymous pages fall back to localStorage.
    page.context.clear_cookies()  # type: ignore[attr-defined]
    page.evaluate("() => localStorage.setItem('theme', 'light')")  # type: ignore[attr-defined]
    page.goto(
        page.url.split("/", 3)[0] + "//" + page.url.split("/")[2] + "/login",
        wait_until="load",
    )  # type: ignore[attr-defined]
    assert page.evaluate("() => window.__themeAtDcl") == "light"  # type: ignore[attr-defined]
