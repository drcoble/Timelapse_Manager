"""Navigation / IA guardrails: flattened nav, role-based items, role badge.

The left nav is flattened (no section labels) with SVG icons; admins get an
extra divider + admin links; the Operator role badge is suppressed.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

MAIN_ITEMS = 7  # Dashboard, Cameras, Projects, Frames, Renders, Events, About
# Notification settings live as a tab on the Settings page, and the audit log is
# now a tab on the Events page, so neither has a standalone nav entry.
ADMIN_ITEMS = 2  # Users, Settings


def _nav_item_count(html: str) -> int:
    return html.count('class="nav-item')


def test_section_labels_removed(admin_client: TestClient) -> None:
    html = admin_client.get("/").text
    assert "nav-section-label" not in html


def test_nav_uses_svg_icons(admin_client: TestClient) -> None:
    html = admin_client.get("/").text
    for icon in ("#icon-dashboard", "#icon-camera", "#icon-capture", "#icon-frame"):
        assert icon in html


def test_admin_sees_all_items(admin_client: TestClient) -> None:
    html = admin_client.get("/").text
    assert _nav_item_count(html) == MAIN_ITEMS + ADMIN_ITEMS
    assert "/users" in html
    assert "/settings" in html
    # The standalone Notifications nav link was retired (now a Settings tab), and
    # the standalone Audit Log nav link was retired (now an Events-page tab).
    assert 'href="/notification-settings"' not in html
    assert 'href="/events/audit"' not in html


def test_operator_sees_only_main_items(operator_client: TestClient) -> None:
    html = operator_client.get("/").text
    assert _nav_item_count(html) == MAIN_ITEMS
    assert "admin-nav" not in html


def test_viewer_sees_only_main_items(viewer_client: TestClient) -> None:
    html = viewer_client.get("/").text
    assert _nav_item_count(html) == MAIN_ITEMS
    assert "admin-nav" not in html


def test_role_badge_shown_for_admin(admin_client: TestClient) -> None:
    assert "role-badge" in admin_client.get("/").text


def test_role_badge_shown_for_viewer(viewer_client: TestClient) -> None:
    assert "role-badge" in viewer_client.get("/").text


def test_role_badge_suppressed_for_operator(operator_client: TestClient) -> None:
    assert "role-badge" not in operator_client.get("/").text
