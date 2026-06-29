"""Web tests for the dashboard project card, left nav, scroll-batch data-*
anchors, and the Settings tab order.

These render the real templates through the authenticated web client and assert
on the emitted markup, so they catch template-level regressions without a
browser.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Event, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_project_with_frames(n_frames: int = 75) -> int:
    """Seed a Camera + Project + ``n_frames`` frames; return the project id.

    Distinct, spaced capture timestamps so a batch's oldest-timestamp anchor is
    unambiguous.
    """
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="dash-cam", address="10.0.0.21", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name="Dashboard Project",
            capture_interval_seconds=300,
            lifecycle_state="active",
        )
        db.add(proj)
        db.flush()
        pid = proj.id
        base = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)
        for i in range(n_frames):
            db.add(
                Frame(
                    project_id=pid,
                    sequence_index=i,
                    capture_timestamp=base.replace(minute=0) if i == 0 else base,
                    file_path=f"/frames/{pid}/{i:08d}.jpg",
                    capture_status="captured",
                    origin="captured",
                    lifecycle_state="active",
                )
            )
        return pid


def _seed_events(n_events: int = 60) -> None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name="evt-cam", address="10.0.0.22", protocol="vapix")
        db.add(cam)
        db.flush()
        for i in range(n_events):
            db.add(
                Event(
                    scope="camera",
                    scope_id=cam.id,
                    level="info",
                    message=f"seed event {i}",
                    event_metadata=None,
                )
            )


# ---------------------------------------------------------------------------
# 1. Dashboard project card: Growth rate + Projection, no Uptime
# ---------------------------------------------------------------------------


def test_dashboard_card_shows_growth_and_projection_not_uptime(
    admin_client: TestClient,
) -> None:
    _seed_project_with_frames(5)
    html = admin_client.get("/").text
    assert "project-card" in html, "dashboard did not render a project card"
    # The two new stat cells are present...
    assert "Growth Rate" in html
    assert "Projection" in html
    # ...and the old Uptime stat cell is gone from the card.
    assert "Uptime" not in html


# ---------------------------------------------------------------------------
# 2. Left nav: no standalone Notifications link, no standalone Audit Log link
# ---------------------------------------------------------------------------


def test_left_nav_has_no_standalone_notifications_or_audit_link(
    admin_client: TestClient,
) -> None:
    html = admin_client.get("/").text
    # The standalone nav entry (an anchor to /notification-settings) is gone.
    assert 'href="/notification-settings"' not in html
    # The standalone Audit Log nav entry is gone (audit is now an Events-page tab).
    assert 'href="/events/audit"' not in html


# ---------------------------------------------------------------------------
# 3. Batch fragments expose the data-* anchor contract
# ---------------------------------------------------------------------------


def test_frames_batch_fragment_exposes_anchor_data_attributes(
    admin_client: TestClient,
) -> None:
    pid = _seed_project_with_frames(75)
    # First batch: 60 tiles + a sentinel (75 > 60).
    html = admin_client.get(f"/frames/batch?project_id={pid}").text
    assert "frame-sentinel" in html
    assert 'data-batch-count="60"' in html
    assert "data-oldest-timestamp=" in html
    assert "data-newest-timestamp=" in html
    assert "data-newest-id=" in html


def test_frames_batch_endcap_exposes_anchor_data_attributes(
    admin_client: TestClient,
) -> None:
    # A small project (< one batch) returns an end-cap, not a sentinel; the
    # announcer reads the data-* contract off the end-cap, so it must be present.
    pid = _seed_project_with_frames(10)
    html = admin_client.get(f"/frames/batch?project_id={pid}").text
    assert "frame-end-cap" in html
    assert "frame-sentinel" not in html
    assert 'data-batch-count="10"' in html
    assert "data-oldest-timestamp=" in html


def test_events_batch_fragment_exposes_anchor_data_attributes(
    admin_client: TestClient,
) -> None:
    _seed_events(80)
    # First batch: 75 rows + a sentinel (80 > 75).
    html = admin_client.get("/events/batch").text
    assert "log-sentinel" in html
    assert 'data-batch-count="75"' in html
    assert "data-oldest-timestamp=" in html
    assert "data-newest-timestamp=" in html
    assert "data-newest-id=" in html


# ---------------------------------------------------------------------------
# 4. Settings tab order: System, LDAP, Notifications, Credentials
# ---------------------------------------------------------------------------


def test_settings_tab_dom_order(admin_client: TestClient) -> None:
    html = admin_client.get("/settings").text
    targets = re.findall(r'data-tab-target="#(tab-[a-z]+)"', html)
    assert targets == [
        "tab-system",
        "tab-network",
        "tab-ldap",
        "tab-notifications",
        "tab-credentials",
    ], targets
    # The panels are rendered in the same order so the no-JS document flow and
    # the tablist agree.
    expected_tabs = {
        "tab-system",
        "tab-network",
        "tab-ldap",
        "tab-notifications",
        "tab-credentials",
    }
    panel_order = [
        m for m in re.findall(r'id="(tab-[a-z]+)"', html) if m in expected_tabs
    ]
    assert panel_order == [
        "tab-system",
        "tab-network",
        "tab-ldap",
        "tab-notifications",
        "tab-credentials",
    ], panel_order
