"""Static contracts for the drawer, skip link, nav and touch rules (#70 WP-70-I).

The behaviour is proven in ``tests/e2e/test_drawer_a11y.py`` and
``tests/e2e/test_mobile_ergonomics.py``; these checks run without a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "src" / "timelapse_manager" / "web"


def _read(rel: str) -> str:
    return (WEB / rel).read_text(encoding="utf-8")


def test_ribbon_drag_area_takes_no_browser_touch_gestures() -> None:
    """#200: the scrubber's pointer drags must not be claimed as scrolls."""
    css = _read("static/css/components/time-ribbon.css")
    rule = re.search(r"\.frame-ribbon \.time-ribbon\s*\{([^}]*)\}", css)
    assert rule is not None
    assert "touch-action: none" in rule.group(1)


def test_user_menu_does_not_open_on_hover() -> None:
    """#205: a hover menu opened under a resting pointer and ate the next click."""
    css = _read("static/css/layout.css")
    assert ".user-menu:focus-within .user-dropdown" in css
    assert ".user-menu:hover .user-dropdown" not in css


def test_drawer_starts_closed_inert_and_hidden() -> None:
    html = _read("templates/base.html")
    aside = re.search(r'<aside id="drawer-main"[^>]*>', html, re.S)
    assert aside is not None
    tag = aside.group(0)
    assert "drawer--hidden" in tag
    assert re.search(r"\binert\b", tag)
    assert "visibility: hidden" in _read("static/css/components/drawer.css")


def test_skip_link_precedes_the_app_shell() -> None:
    html = _read("templates/base.html")
    skip = html.index('class="skip-link" href="#main-content"')
    assert skip < html.index('<div class="app-shell">')
    assert 'id="main-content"' in html


def test_nav_renders_aria_current_only_on_the_current_item() -> None:
    nav = _read("templates/_partials/shell/nav.html")
    assert 'aria-current="{%' not in nav
    assert len(re.findall(r'\{% if [^%]+%\}aria-current="page"\{% endif %\}', nav)) == 9
