"""Web tests for the Storage card on the project detail page.

Covers the redesigned "Storage" card (F2):
- Finite campaign: card renders "Projected at Completion" section (not the
  open-ended footnote); "Set an end date" copy is absent.
- Open-ended campaign: card renders the "Set an end date to see a projected
  total." footnote; the "Projected at Completion" section is absent.
- Growth-rate row: renders "&approx; X / day" when frames exist; renders
  "Not enough data yet" when the project has no frames.

The card's data-testid="projected-storage" attribute is asserted in every test
to anchor DOM presence.  Helpers are local so this file does not edit the
shared conftest.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context


def _seed_project(
    *,
    name: str,
    end_date: datetime | None,
    start_date: datetime | None = None,
    interval: int = 60,
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
            lifecycle_state="active",
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
        """Finite campaign + frames: both growth rate and projected total render."""
        start = datetime(2026, 8, 1, 0, 0)
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
