"""The dashboard grid poll answers 204 when nothing changed (#70 F-P1, WP-70-E2).

``GET /partials/projects`` carries ``X-TLM-Poll-Version``, a digest of the
displayed card values. poll.js sends the last version back; a match is an
empty 204, which htmx does not swap. The dashboard page seeds the same version
on ``#projects-grid``, so even the first poll after load is a 204.

Each card's ribbon slot is ``hx-preserve`` with an id keyed on the latest frame
id, so a swap that does happen keeps every loaded ribbon except the one whose
project has a new frame.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.presession_support import session_cookie
from timelapse_manager.db.models import Camera, Frame, Project
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.sessions import hash_token
from timelapse_manager.web.routers import _viewmodels
from timelapse_manager.web.routers._viewmodels import (
    DASHBOARD_CARD_FIELDS,
    RIBBON_MIN_BUCKET_SECONDS,
    _project_views,
    dashboard_poll_version,
    ribbon_key,
)

_HEADER = "X-TLM-Poll-Version"
_CARD_TEMPLATE = (
    Path(_viewmodels.__file__).resolve().parent.parent
    / "templates"
    / "_partials"
    / "project_status_card.html"
)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _seed_project(name: str = "poll-project") -> int:
    """An active project with no frames.

    Its campaign starts in the future, so its next capture is the fixed start
    rather than *now*, which moves (at minute precision) during a test.
    """
    with session_scope(get_context().session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=f"/tmp/{name}",
            start_date=datetime.datetime(2099, 1, 1),
        )
        db.add(proj)
        db.flush()
        return proj.id


def _add_frame(project_id: int, sequence_index: int) -> int:
    with session_scope(get_context().session_factory) as db:
        frame = Frame(
            project_id=project_id,
            sequence_index=sequence_index,
            capture_timestamp=_utcnow(),
            file_path=f"f{sequence_index}.jpg",
            origin="captured",
            lifecycle_state="active",
        )
        db.add(frame)
        db.flush()
        return frame.id


# A fixed clock for the ribbon key's time bucket, so no test can straddle an
# hour boundary between two requests. Tests that move time patch it again.
_T0 = datetime.datetime(2026, 10, 28, 12, 0, 5, tzinfo=datetime.UTC)


@pytest.fixture(autouse=True)
def _fixed_ribbon_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_viewmodels, "_ribbon_clock", lambda: _T0)


def _ribbon_slot_ids(html: str) -> list[str]:
    """The ribbon slot ids, without the key's trailing digest."""
    return re.findall(r'id="(ribbon-slot-[0-9]+-[0-9]+)-[0-9a-f]{8}"', html)


class TestPollVersionRoundTrip:
    def test_first_poll_returns_the_grid_and_a_version(
        self, admin_client: TestClient
    ) -> None:
        _seed_project()
        resp = admin_client.get("/partials/projects")
        assert resp.status_code == 200
        assert re.fullmatch(r"[0-9a-f]{16}", resp.headers[_HEADER])
        assert 'class="project-grid"' in resp.text

    def test_matching_version_is_an_empty_204(self, admin_client: TestClient) -> None:
        _seed_project()
        version = admin_client.get("/partials/projects").headers[_HEADER]
        again = admin_client.get("/partials/projects", headers={_HEADER: version})
        assert again.status_code == 204
        assert again.content == b""
        assert again.headers[_HEADER] == version

    def test_stale_version_gets_the_grid(self, admin_client: TestClient) -> None:
        _seed_project()
        version = admin_client.get("/partials/projects").headers[_HEADER]
        resp = admin_client.get(
            "/partials/projects", headers={_HEADER: "0000000000000000"}
        )
        assert resp.status_code == 200
        assert resp.headers[_HEADER] == version

    def test_a_new_frame_changes_the_version(self, admin_client: TestClient) -> None:
        project_id = _seed_project()
        version = admin_client.get("/partials/projects").headers[_HEADER]
        _add_frame(project_id, 1)
        resp = admin_client.get("/partials/projects", headers={_HEADER: version})
        assert resp.status_code == 200
        assert resp.headers[_HEADER] != version

    def test_empty_dashboard_also_answers_204(self, admin_client: TestClient) -> None:
        first = admin_client.get("/partials/projects")
        assert first.status_code == 200
        assert "No active projects" in first.text
        again = admin_client.get(
            "/partials/projects", headers={_HEADER: first.headers[_HEADER]}
        )
        assert again.status_code == 204

    def test_dashboard_page_seeds_the_grid_version(
        self, admin_client: TestClient
    ) -> None:
        _seed_project()
        page = admin_client.get("/")
        seeded = re.search(
            r'id="projects-grid"[^>]*data-poll-version="([0-9a-f]{16})"', page.text
        )
        assert seeded is not None
        resp = admin_client.get("/partials/projects", headers={_HEADER: seeded[1]})
        assert resp.status_code == 204

    def test_archived_projects_do_not_change_the_seeded_version(
        self, admin_client: TestClient
    ) -> None:
        """The page renders archived cards too, but the poll covers active only."""
        _seed_project("active-one")
        archived = _seed_project("archived-one")
        with session_scope(get_context().session_factory) as db:
            proj = db.get(Project, archived)
            assert proj is not None
            proj.lifecycle_state = "archived"
        page = admin_client.get("/")
        seeded = re.search(r'data-poll-version="([0-9a-f]{16})"', page.text)
        assert seeded is not None
        resp = admin_client.get("/partials/projects", headers={_HEADER: seeded[1]})
        assert resp.status_code == 204


class TestRibbonSlot:
    def test_slot_id_carries_the_latest_frame_id(
        self, admin_client: TestClient
    ) -> None:
        project_id = _seed_project()
        assert _ribbon_slot_ids(admin_client.get("/partials/projects").text) == [
            f"ribbon-slot-{project_id}-0"
        ]
        _add_frame(project_id, 1)
        latest = _add_frame(project_id, 2)
        html = admin_client.get("/partials/projects").text
        assert _ribbon_slot_ids(html) == [f"ribbon-slot-{project_id}-{latest}"]
        slot = re.search(r'<div class="time-ribbon-slot"[^>]*>', html)
        assert slot is not None
        assert 'hx-preserve="true"' in slot[0]
        assert 'hx-trigger="load"' in slot[0]

    def test_full_page_and_single_card_use_the_same_slot_id(
        self, admin_client: TestClient
    ) -> None:
        project_id = _seed_project()
        latest = _add_frame(project_id, 1)
        want = [f"ribbon-slot-{project_id}-{latest}"]
        assert _ribbon_slot_ids(admin_client.get("/").text) == want
        status = admin_client.get(f"/partials/projects/{project_id}/status")
        assert _ribbon_slot_ids(status.text) == want


class TestVersionIsPassiveAndAuthenticated:
    """The 204 short cut changes neither the idle clock nor the sign-out."""

    def _token(self, client: TestClient) -> str:
        return session_cookie(client) or next(iter(client.cookies.values()))

    def _set_last_active(self, token: str, value: datetime.datetime) -> None:
        with session_scope(get_context().session_factory) as db:
            row = db.query(SessionRow).filter_by(token_hash=hash_token(token)).one()
            row.last_active = value

    def _last_active(self, token: str) -> datetime.datetime | None:
        with session_scope(get_context().session_factory) as db:
            row = db.query(SessionRow).filter_by(token_hash=hash_token(token)).one()
            return row.last_active

    def test_passive_204_does_not_touch_the_session(
        self, admin_client: TestClient
    ) -> None:
        _seed_project()
        version = admin_client.get("/partials/projects").headers[_HEADER]
        token = self._token(admin_client)
        stale = _utcnow() - datetime.timedelta(minutes=5)
        self._set_last_active(token, stale)
        resp = admin_client.get(
            "/partials/projects",
            headers={_HEADER: version, "X-TLM-Passive": "1", "X-TLM-Idle": "300"},
        )
        assert resp.status_code == 204
        assert self._last_active(token) == stale

    def test_idle_expired_poll_with_a_matching_version_still_signs_out(
        self, admin_client: TestClient
    ) -> None:
        """Both answers are 204; HX-Redirect tells them apart. Auth runs first."""
        _seed_project()
        version = admin_client.get("/partials/projects").headers[_HEADER]
        token = self._token(admin_client)
        self._set_last_active(token, _utcnow() - datetime.timedelta(minutes=31))
        resp = admin_client.get(
            "/partials/projects",
            headers={
                _HEADER: version,
                "X-TLM-Passive": "1",
                "X-TLM-Idle": "1860",
                "HX-Request": "true",
                "HX-Current-URL": "https://testserver/",
            },
        )
        assert resp.status_code == 204
        assert resp.headers["HX-Redirect"].startswith("/login")
        assert _HEADER not in resp.headers

    def test_anonymous_poll_is_not_answered_with_a_version(
        self, anon_client: TestClient
    ) -> None:
        resp = anon_client.get(
            "/partials/projects",
            headers={_HEADER: "0000000000000000"},
            follow_redirects=False,
        )
        assert resp.status_code in (302, 303, 401)
        assert _HEADER not in resp.headers


class TestDigest:
    def _view(self, admin_client: TestClient) -> _viewmodels._ProjectView:
        _seed_project()
        with session_scope(get_context().session_factory) as db:
            return _project_views(db)[0]

    def test_volatile_fields_the_card_does_not_show_are_ignored(
        self, admin_client: TestClient
    ) -> None:
        view = self._view(admin_client)
        base = dashboard_poll_version([view], role="admin", viewer_timezone=None)
        moved = dataclasses.replace(
            view,
            uptime_seconds=(view.uptime_seconds or 0) + 31,
            uptime_display="31s",
            projected_frames_remaining=12345,
        )
        assert (
            dashboard_poll_version([moved], role="admin", viewer_timezone=None) == base
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("operational_status", "error"),
            ("frame_count", 99),
            ("is_overdue", True),
            ("ribbon_key", "42-deadbeef"),
            ("latest_frame_url", "/projects/1/frames/42/image"),
        ],
    )
    def test_displayed_fields_change_the_version(
        self, admin_client: TestClient, field: str, value: object
    ) -> None:
        view = self._view(admin_client)
        base = dashboard_poll_version([view], role="admin", viewer_timezone=None)
        changed = dataclasses.replace(view, **{field: value})
        assert (
            dashboard_poll_version([changed], role="admin", viewer_timezone=None)
            != base
        )

    def test_role_and_timezone_change_the_version(
        self, admin_client: TestClient
    ) -> None:
        view = self._view(admin_client)
        base = dashboard_poll_version([view], role="admin", viewer_timezone=None)
        assert (
            dashboard_poll_version([view], role="viewer", viewer_timezone=None) != base
        )
        assert (
            dashboard_poll_version([view], role="admin", viewer_timezone="Europe/Paris")
            != base
        )

    def test_every_field_the_card_reads_is_digested(self) -> None:
        """A field added to the card template must be added to the digest."""
        source = _CARD_TEMPLATE.read_text("utf-8")
        used = set(re.findall(r"\bproject\.([a-z_]+)", source))
        assert used, "the card template no longer reads project.* fields?"
        missing = used - set(DASHBOARD_CARD_FIELDS)
        assert not missing, (
            f"project_status_card.html reads {sorted(missing)}; add them to "
            "DASHBOARD_CARD_FIELDS so a change there refreshes the dashboard poll"
        )

    def test_digest_fields_exist_on_the_view(self) -> None:
        names = {f.name for f in dataclasses.fields(_viewmodels._ProjectView)}
        assert set(DASHBOARD_CARD_FIELDS) <= names


class TestRibbonKey:
    """What reloads a card ribbon: its frames, its bounds and the clock."""

    def _key(self, **overrides: object) -> str:
        args: dict[str, object] = {
            "latest_frame_id": 7,
            "frame_count": 7,
            "start_date": datetime.datetime(2026, 10, 1),
            "end_date": None,
            "now": _T0,
        }
        args.update(overrides)
        return ribbon_key(**args)  # type: ignore[arg-type]

    def test_starts_with_the_latest_frame_id(self) -> None:
        assert re.fullmatch(r"7-[0-9a-f]{8}", self._key())
        assert self._key(latest_frame_id=None).startswith("0-")

    def test_same_within_the_hour_new_after_it(self) -> None:
        key = self._key()
        later = _T0 + datetime.timedelta(minutes=50)
        assert self._key(now=later) == key
        rolled = _T0.replace(minute=0, second=0) + datetime.timedelta(hours=1)
        assert self._key(now=rolled) != key

    @pytest.mark.parametrize(
        "change",
        [
            {"latest_frame_id": 8},
            {"frame_count": 6},  # a frame deleted under the latest one
            {"start_date": datetime.datetime(2026, 9, 1)},
            {"end_date": datetime.datetime(2026, 12, 1)},
        ],
    )
    def test_frames_and_bounds_change_it(self, change: dict[str, object]) -> None:
        assert self._key(**change) != self._key()

    def test_a_long_campaign_uses_a_coarser_bucket(self) -> None:
        """Two years over 1000 units is ~17.5 h a unit: a 32 h bucket."""
        start = datetime.datetime(2024, 10, 28)
        bucket = _viewmodels._ribbon_bucket_seconds(start, None, _T0)
        assert bucket == 32 * RIBBON_MIN_BUCKET_SECONDS
        base = _T0.replace(minute=0, second=0)
        aligned = datetime.datetime.fromtimestamp(
            (int(base.timestamp()) // bucket) * bucket, datetime.UTC
        )
        key = self._key(start_date=start, now=aligned)
        assert (
            self._key(start_date=start, now=aligned + datetime.timedelta(hours=31))
            == key
        )
        assert (
            self._key(start_date=start, now=aligned + datetime.timedelta(hours=32))
            != key
        )

    def test_no_start_date_uses_the_hour(self) -> None:
        assert _viewmodels._ribbon_bucket_seconds(None, None, _T0) == 3600

    def test_an_ended_campaign_never_reloads_on_time(self) -> None:
        end = datetime.datetime(2026, 10, 20)
        key = self._key(end_date=end)
        assert self._key(end_date=end, now=_T0 + datetime.timedelta(days=30)) == key


class TestRibbonReloadOnTheGrid:
    """The grid poll: unchanged means 204; a rollover means a new slot."""

    def test_unchanged_poll_keeps_the_slot_and_answers_204(
        self, admin_client: TestClient
    ) -> None:
        project_id = _seed_project()
        first = admin_client.get("/partials/projects")
        again = admin_client.get(
            "/partials/projects", headers={_HEADER: first.headers[_HEADER]}
        )
        assert again.status_code == 204
        assert _ribbon_slot_ids(first.text) == [f"ribbon-slot-{project_id}-0"]

    def test_bucket_rollover_gives_a_new_slot(
        self, admin_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_project()
        first = admin_client.get("/partials/projects")
        slot = re.search(r'id="(ribbon-slot-[^"]+)"', first.text)
        assert slot is not None
        monkeypatch.setattr(
            _viewmodels, "_ribbon_clock", lambda: _T0 + datetime.timedelta(hours=1)
        )
        resp = admin_client.get(
            "/partials/projects", headers={_HEADER: first.headers[_HEADER]}
        )
        assert resp.status_code == 200
        rolled = re.search(r'id="(ribbon-slot-[^"]+)"', resp.text)
        assert rolled is not None and rolled[1] != slot[1]
        # The project id and latest frame id prefix is unchanged.
        assert rolled[1].rsplit("-", 1)[0] == slot[1].rsplit("-", 1)[0]
