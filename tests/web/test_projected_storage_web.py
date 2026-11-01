"""Web tests for the Storage card on the project detail page.

Covers the redesigned "Storage" card (F2):
- Finite campaign: card renders "Projected at Completion" section (not the
  open-ended footnote); "Set an end date" copy is absent.
- Open-ended campaign: card renders the "Set an end date to see a projected
  total." footnote; the "Projected at Completion" section is absent.
- Growth-rate row: renders "&approx; X / day" when frames exist; renders
  "Not enough data yet" when the project has no frames.
- #22: an anchor-only project shows its rate on the detail page and the
  dashboard card; a paused, low-disk or stopped project shows none (D12), while
  one only waiting for its schedule window keeps it.

The card's data-testid="projected-storage" attribute is asserted in every test
to anchor DOM presence.  Helpers are local so this file does not edit the
shared conftest.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.routers._viewmodels import _project_view

NOON_ANCHOR = [{"id": "noon", "kind": "clock", "time": "12:00", "enabled": True}]


@dataclass
class _FakeState:
    """A live CaptureState stand-in carrying just the fields the view reads."""

    state: str
    pause_reason: str | None = None
    last_capture_at: datetime | None = None
    last_error: str | None = None
    last_error_at: datetime | None = None
    started_at: datetime | None = None


def _install_supervisor(state: _FakeState | None) -> Any:
    """Make the live supervisor report *state* for every project; return the old one."""
    ctx = get_context()
    previous = ctx.capture_supervisor
    sup = MagicMock()
    sup.state_for_project.return_value = state
    ctx.capture_supervisor = sup
    return previous


@pytest.fixture
def running_supervisor(admin_client: TestClient) -> Iterator[None]:
    """A live runner reporting ``running``: without one a project reads "stopped",
    which shows no growth rate (#22 D12)."""
    previous = _install_supervisor(_FakeState(state="running"))
    try:
        yield
    finally:
        get_context().capture_supervisor = previous


def _seed_project(
    *,
    name: str,
    end_date: datetime | None,
    start_date: datetime | None = None,
    interval: int | None = 60,
    anchors: list[dict[str, Any]] | None = None,
    lifecycle_state: str = "active",
) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=f"{name}-cam",
            address="127.0.0.1",
            protocol="vapix",
            snapshot_uri="http://127.0.0.1/snap",
        )
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            capture_interval_seconds=interval,
            exact_time_anchors=anchors,
            lifecycle_state=lifecycle_state,
            start_date=start_date,
            end_date=end_date,
        )
        db.add(proj)
        db.flush()
        return proj.id


def _seed_frames(project_id: int, count: int, size: int = 1000) -> None:
    """Add ``count`` active frames with a known file_size_bytes to ``project_id``."""
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        for seq in range(1, count + 1):
            ts = datetime(2026, 1, 1, 0, seq, tzinfo=UTC).replace(tzinfo=None)
            db.add(
                Frame(
                    project_id=project_id,
                    sequence_index=seq,
                    capture_timestamp=ts,
                    file_path=f"/frames/{seq:08d}.jpg",
                    width=1,
                    height=1,
                    file_size_bytes=size,
                    capture_status="captured",
                    origin="captured",
                    lifecycle_state="active",
                )
            )
        project = db.get(Project, project_id)
        assert project is not None
        project.frame_count = count


class TestProjectedStorageCard:
    def test_finite_campaign_renders_projection(self, admin_client: TestClient) -> None:
        """Finite campaign (has end_date) shows "Projected at Completion" section.

        With zero frames the estimator falls back to the 512 KB default and
        still produces a non-None projected_bytes, so projected_open_ended is
        False. The card must carry the "Projected at Completion" label and must
        NOT show the open-ended footnote.
        """
        start = datetime(2026, 7, 1, 0, 0)
        pid = _seed_project(
            name="Web Projected Finite",
            start_date=start,
            end_date=start + timedelta(days=1),
        )
        resp = admin_client.get(f"/projects/{pid}")
        assert resp.status_code == 200, resp.text
        html = resp.text
        # Card title changed to "Storage" (from old "Projected Storage").
        assert 'data-testid="projected-storage"' in html
        assert "Storage" in html
        # Finite row label (old label was "Frames Remaining").
        assert "Projected at Completion" in html
        # Open-ended footnote must be absent for a finite campaign.
        assert "Set an end date to see a projected total." not in html

    def test_open_ended_campaign_renders_notice(self, admin_client: TestClient) -> None:
        """Open-ended campaign (no end_date) shows the "Set an end date" footnote.

        projected_open_ended is True, so the "Projected at Completion" row is
        suppressed and the footnote replaces it.
        """
        pid = _seed_project(name="Web Projected Open", end_date=None)
        resp = admin_client.get(f"/projects/{pid}")
        assert resp.status_code == 200, resp.text
        html = resp.text
        assert 'data-testid="projected-storage"' in html
        assert "Storage" in html
        # Open-ended footnote must appear.
        assert "Set an end date to see a projected total." in html
        # The projection row must not appear.
        assert "Projected at Completion" not in html


@pytest.mark.usefixtures("running_supervisor")
class TestGrowthRateRow:
    def test_no_frames_shows_not_enough_data(self, admin_client: TestClient) -> None:
        """A project with zero frames renders the "Not enough data yet" placeholder.

        estimate_growth_rate_bytes_per_day returns None when frame_count is 0,
        so the template's else-branch shows the dim placeholder.
        """
        pid = _seed_project(name="Growth No Frames", end_date=None)
        resp = admin_client.get(f"/projects/{pid}")
        assert resp.status_code == 200, resp.text
        html = resp.text
        assert 'data-testid="projected-storage"' in html
        assert "Not enough data yet" in html
        assert "Growth Rate" in html

    def test_frames_present_shows_growth_rate(self, admin_client: TestClient) -> None:
        """A project with captured frames renders the measured growth rate.

        4 frames at 1000 bytes each, 60 s interval → 1 440 000 bytes/day
        (1000 * 86400 / 60).  The template renders "&approx; <rate> / day";
        we assert the rate value and its "/ day" suffix are present.
        """
        pid = _seed_project(name="Growth With Frames", end_date=None)
        _seed_frames(pid, count=4, size=1000)
        resp = admin_client.get(f"/projects/{pid}")
        assert resp.status_code == 200, resp.text
        html = resp.text
        assert 'data-testid="projected-storage"' in html
        assert "Growth Rate" in html
        # Rate rendered as "≈ X / day"; "Not enough data" must be absent.
        assert "/ day" in html
        assert "Not enough data yet" not in html

    def test_finite_campaign_with_frames_shows_both_rate_and_projection(
        self, admin_client: TestClient
    ) -> None:
        """Finite campaign + frames: both growth rate and projected total render.

        The campaign lies in the future: a campaign whose end date has passed
        plans no capture and shows no rate (#22).
        """
        start = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(
            days=1
        )
        pid = _seed_project(
            name="Growth Finite Frames",
            start_date=start,
            end_date=start + timedelta(days=1),
        )
        _seed_frames(pid, count=4, size=1000)
        resp = admin_client.get(f"/projects/{pid}")
        assert resp.status_code == 200, resp.text
        html = resp.text
        assert 'data-testid="projected-storage"' in html
        # Both growth-rate row and projection row appear.
        assert "/ day" in html
        assert "Projected at Completion" in html
        assert "Not enough data yet" not in html
        assert "Set an end date to see a projected total." not in html


def _view(project_id: int) -> Any:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        project = db.get(Project, project_id)
        assert project is not None
        return _project_view(db, project)


@pytest.mark.usefixtures("admin_client")  # installs the application context
class TestGrowthRateFromSchedule:
    """#22: the rate counts the whole schedule, and only while capturing (D12)."""

    def test_anchor_only_view_shows_one_frame_a_day(self) -> None:
        pid = _seed_project(
            name="GR anchor-only view",
            end_date=None,
            interval=None,
            anchors=NOON_ANCHOR,
        )
        _seed_frames(pid, count=3, size=777)
        previous = _install_supervisor(_FakeState(state="running"))
        try:
            view = _view(pid)
        finally:
            get_context().capture_supervisor = previous
        assert view.capture_planned is True
        assert view.next_capture_at_raw is not None
        # One 12:00 anchor a day x the 777-byte average frame.
        assert view.growth_rate_display == "777 B / day"

    def test_anchor_only_detail_and_card_show_rate(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_project(
            name="GR anchor-only page",
            end_date=None,
            interval=None,
            anchors=NOON_ANCHOR,
        )
        _seed_frames(pid, count=2, size=733)
        previous = _install_supervisor(_FakeState(state="running"))
        try:
            detail = admin_client.get(f"/projects/{pid}")
            dashboard = admin_client.get("/")
        finally:
            get_context().capture_supervisor = previous
        assert detail.status_code == 200, detail.text
        assert "733 B / day" in detail.text
        assert "Not enough data yet" not in detail.text
        assert dashboard.status_code == 200, dashboard.text
        assert "~733 B / day" in dashboard.text

    def test_window_closed_keeps_rate(self) -> None:
        # Idle because the schedule window is closed reads "paused" on the card,
        # but the project still plans its next capture, so the rate stays.
        pid = _seed_project(name="GR window closed", end_date=None, interval=3600)
        _seed_frames(pid, count=2, size=1000)
        previous = _install_supervisor(_FakeState(state="idle", pause_reason="window"))
        try:
            view = _view(pid)
        finally:
            get_context().capture_supervisor = previous
        assert view.operational_status == "paused"
        assert view.capture_planned is True
        assert view.growth_rate_display == "23.4 KB / day"  # 24 x 1000 B

    @pytest.mark.parametrize(
        ("lifecycle_state", "state", "expected_status"),
        [
            ("paused", None, "paused"),
            ("active", _FakeState(state="idle", pause_reason="low_disk"), "lowdisk"),
            ("active", None, "stopped"),
            ("active", _FakeState(state="stopped"), "stopped"),
        ],
    )
    def test_not_capturing_shows_no_rate(
        self,
        admin_client: TestClient,
        lifecycle_state: str,
        state: _FakeState | None,
        expected_status: str,
    ) -> None:
        # D12 (owner): paused, low disk and stopped plan no capture -> no rate,
        # interval projects included (a change from 0.1.x for paused ones).
        name = f"GR not capturing {lifecycle_state} {expected_status} {id(state)}"
        pid = _seed_project(
            name=name, end_date=None, interval=60, lifecycle_state=lifecycle_state
        )
        _seed_frames(pid, count=4, size=1000)
        previous = _install_supervisor(state)
        try:
            view = _view(pid)
            detail = admin_client.get(f"/projects/{pid}")
        finally:
            get_context().capture_supervisor = previous
        assert view.operational_status == expected_status
        assert view.capture_planned is False
        assert view.growth_rate_display is None
        assert detail.status_code == 200, detail.text
        assert "None while not capturing" in detail.text
        assert "/ day" not in detail.text
