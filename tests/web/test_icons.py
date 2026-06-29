"""Icon-sprite guardrails: every symbol is defined and the sprite is included."""

from __future__ import annotations

from fastapi.testclient import TestClient

# Every symbol the UI relies on. Dropping or renaming one would silently break
# every <use href="#..."> that references it.
ICON_IDS = [
    "icon-logo",
    "icon-capture",
    "icon-camera",
    "icon-frame",
    "icon-render",
    "icon-events",
    "icon-dashboard",
    "icon-users",
    "icon-settings",
    "icon-alert",
    "icon-about",
    "icon-archive",
    "icon-play",
    "icon-pause",
    "icon-stop",
    "icon-device",
    "icon-download",
    "icon-query",
]


def test_sprite_present_on_every_page(admin_client: TestClient) -> None:
    html = admin_client.get("/").text
    assert 'class="icon-sprite"' in html


def test_all_symbols_defined(admin_client: TestClient) -> None:
    html = admin_client.get("/").text
    missing = [i for i in ICON_IDS if f'id="{i}"' not in html]
    assert not missing, f"icon symbols missing from sprite: {missing}"


def test_header_logo_uses_mark(admin_client: TestClient) -> None:
    html = admin_client.get("/").text
    assert 'href="#icon-logo"' in html
