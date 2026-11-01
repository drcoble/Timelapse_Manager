"""The viewer's time zone, end to end on the server side (#70 F-R4, F-R5).

F-R4: ``POST /account/timezone`` with ``source=auto`` (the browser shim) only
fills an empty preference; ``source=manual`` (the Preferences page) replaces it,
clears it when blank, and rejects an unknown zone with a 422.

F-R5: one rule for every user-entered time: a naive ``datetime-local`` value is
wall-clock time in the viewer's zone, and an offset-aware ISO value keeps its
offset. Covered for the frames ``?at=`` jump, the events ``?at=`` jump and the
milestone timestamp, plus the zone labels the pages print.

Every instant here is a fixed January or DST-change date, so no assertion
depends on today's date. A label that stands for many dates names the zone (#125).
"""

from __future__ import annotations

import datetime
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera, Event, Frame, Milestone, Project, User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.dependencies import (
    _tz_label_filter,
    is_valid_timezone,
    viewer_local_to_utc,
    viewer_zone,
)
from timelapse_manager.web.routers.events import _parse_at as events_parse_at
from timelapse_manager.web.routers.frames import _parse_at as frames_parse_at

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_NY = "America/New_York"


def _set_zone(username: str, tz: str | None) -> None:
    """Store ``tz`` as ``username``'s viewer zone, straight in the DB."""
    with session_scope(get_context().session_factory) as db:
        user = db.query(User).filter(User.username == username).one()
        user.viewer_timezone = tz


def _zone_of(username: str) -> str | None:
    with session_scope(get_context().session_factory) as db:
        return db.query(User).filter(User.username == username).one().viewer_timezone


def _post_zone(client: TestClient, **data: str) -> int:
    csrf = csrf_of(client, "/")
    resp = client.post(
        "/account/timezone", data={**data, "csrf_token": csrf}, headers=_FORM
    )
    return resp.status_code


def _epoch(t: datetime.datetime) -> int:
    return int(t.timestamp())


# ---------------------------------------------------------------------------
# Helpers in dependencies.py
# ---------------------------------------------------------------------------


class TestZoneHelpers:
    @pytest.mark.parametrize("name", [None, "", "Not/A_Zone", "../etc", "a" * 300])
    def test_bad_names_resolve_to_utc(self, name: str | None) -> None:
        assert viewer_zone(name) is datetime.UTC

    @pytest.mark.parametrize(
        ("name", "ok"),
        [
            (_NY, True),
            ("UTC", True),
            ("Europe/Paris", True),
            ("", False),
            ("Not/A_Zone", False),
            ("../x", False),
            ("/etc/passwd", False),
            ("localtime", False),
            ("a" * 300, False),
        ],
    )
    def test_is_valid_timezone(self, name: str, ok: bool) -> None:
        assert is_valid_timezone(name) is ok

    def test_localtime_refused_when_the_host_lists_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Debian without a tzdata index lists ``localtime`` as a zone (CI 479)."""
        import zoneinfo

        from timelapse_manager.web import dependencies

        listed = {"UTC", "Europe/Paris", "localtime", "posixrules"}
        monkeypatch.setattr(zoneinfo, "available_timezones", lambda: set(listed))
        dependencies._known_timezones.cache_clear()
        try:
            assert is_valid_timezone("Europe/Paris") is True
            assert is_valid_timezone("localtime") is False
            assert is_valid_timezone("posixrules") is False
        finally:
            dependencies._known_timezones.cache_clear()

    def test_naive_is_read_in_viewer_zone(self) -> None:
        got = viewer_local_to_utc(datetime.datetime(2026, 1, 1, 8, 0), _NY)
        assert got == datetime.datetime(2026, 1, 1, 13, 0, tzinfo=datetime.UTC)

    def test_aware_keeps_its_offset(self) -> None:
        aware = datetime.datetime(2026, 1, 1, 8, 0, tzinfo=datetime.UTC)
        assert viewer_local_to_utc(aware, _NY) == aware

    def test_no_zone_means_utc(self) -> None:
        got = viewer_local_to_utc(datetime.datetime(2026, 1, 1, 8, 0), None)
        assert got == datetime.datetime(2026, 1, 1, 8, 0, tzinfo=datetime.UTC)

    def test_repeated_hour_is_the_first_occurrence(self) -> None:
        """NY 2026-11-01 01:30 happens twice; fold=0 is the EDT one (05:30Z)."""
        got = viewer_local_to_utc(datetime.datetime(2026, 11, 1, 1, 30), _NY)
        assert got == datetime.datetime(2026, 11, 1, 5, 30, tzinfo=datetime.UTC)

    def test_skipped_hour_uses_the_offset_before_the_gap(self) -> None:
        """NY 2026-03-08 02:30 never happens; it lands at 07:30Z (EST offset)."""
        got = viewer_local_to_utc(datetime.datetime(2026, 3, 8, 2, 30), _NY)
        assert got == datetime.datetime(2026, 3, 8, 7, 30, tzinfo=datetime.UTC)

    @pytest.mark.parametrize("parse", [frames_parse_at, events_parse_at])
    def test_parse_at_reads_naive_in_viewer_zone(self, parse: object) -> None:
        fn = parse  # type: ignore[assignment]
        want = datetime.datetime(2026, 1, 1, 13, 0, tzinfo=datetime.UTC)
        assert fn("2026-01-01T08:00", _NY) == want  # type: ignore[operator]
        assert fn("2026-01-01T13:00+00:00", _NY) == want  # type: ignore[operator]
        assert fn("2026-01-01T13:00", None) == want  # type: ignore[operator]
        assert fn("garbage", _NY) is None  # type: ignore[operator]
        assert fn(None, _NY) is None  # type: ignore[operator]

    def test_tz_label_with_a_date_uses_that_dates_abbreviation(self) -> None:
        """#125: the abbreviation belongs to the value's own date, not today's."""
        winter = datetime.datetime(2026, 1, 15, tzinfo=datetime.UTC)
        summer = datetime.datetime(2026, 7, 15, tzinfo=datetime.UTC)
        assert _tz_label_filter(_NY, winter) == "EST"
        assert _tz_label_filter(_NY, summer) == "EDT"
        # A naive datetime is stored UTC, like every stored value.
        assert _tz_label_filter(_NY, winter.replace(tzinfo=None)) == "EST"
        assert _tz_label_filter(None, winter) == "UTC"
        assert _tz_label_filter("Not/A_Zone", winter) == "UTC"
        # A zone with only a numeric abbreviation is named instead.
        assert _tz_label_filter("Pacific/Kiritimati", winter) == "Pacific/Kiritimati"

    def test_tz_label_without_a_date_names_the_zone(self) -> None:
        """#125: a label for many dates never shows today's EDT beside January."""
        assert _tz_label_filter(_NY) == _NY
        assert _tz_label_filter(None) == "UTC"
        assert _tz_label_filter("Not/A_Zone") == "UTC"


# ---------------------------------------------------------------------------
# F-R4: /account/timezone source rules
# ---------------------------------------------------------------------------


class TestTimezoneSource:
    def test_auto_fills_an_empty_zone(self, admin_client: TestClient) -> None:
        assert _post_zone(admin_client, timezone=_NY, source="auto") == 204
        assert _zone_of("admin") == _NY

    def test_auto_never_overwrites_a_stored_zone(
        self, admin_client: TestClient
    ) -> None:
        _set_zone("admin", "Europe/Paris")
        assert _post_zone(admin_client, timezone=_NY, source="auto") == 204
        assert _zone_of("admin") == "Europe/Paris"

    def test_missing_source_is_treated_as_auto(self, admin_client: TestClient) -> None:
        """An old tab's shim (no source field) cannot overwrite a choice."""
        _set_zone("admin", "Europe/Paris")
        assert _post_zone(admin_client, timezone=_NY) == 204
        assert _zone_of("admin") == "Europe/Paris"

    def test_auto_ignores_an_unknown_zone(self, admin_client: TestClient) -> None:
        assert _post_zone(admin_client, timezone="../x", source="auto") == 204
        assert _zone_of("admin") is None

    def test_manual_replaces_a_stored_zone(self, admin_client: TestClient) -> None:
        _set_zone("admin", _NY)
        assert _post_zone(admin_client, timezone="Europe/Paris", source="manual") == 204
        assert _zone_of("admin") == "Europe/Paris"

    def test_manual_blank_clears_the_zone(self, admin_client: TestClient) -> None:
        _set_zone("admin", _NY)
        assert _post_zone(admin_client, timezone="", source="manual") == 204
        assert _zone_of("admin") is None

    @pytest.mark.parametrize("bad", ["Not/A_Zone", "../x", "/etc/passwd", "a" * 300])
    def test_manual_unknown_zone_is_a_422(
        self, admin_client: TestClient, bad: str
    ) -> None:
        """A bad name is refused with a reason, never a 500, and nothing changes."""
        _set_zone("admin", _NY)
        csrf = csrf_of(admin_client, "/")
        resp = admin_client.post(
            "/account/timezone",
            data={"timezone": bad, "source": "manual", "csrf_token": csrf},
            headers=_FORM,
        )
        assert resp.status_code == 422
        assert "Unknown time zone" in resp.json()["detail"]
        assert _zone_of("admin") == _NY

    def test_preferences_page_loads_preferences_js(
        self, admin_client: TestClient
    ) -> None:
        html = admin_client.get("/account/preferences").text
        assert re.search(
            r'<script src="/static/js/preferences\.js\?v=\w+" defer>', html
        )
        assert 'id="timezone-browser-hint"' in html
        # The page script moved out: no inline script posts the preferences.
        assert "Promise.all" not in html


# ---------------------------------------------------------------------------
# F-R5: the jump anchors, milestones and labels
# ---------------------------------------------------------------------------

# 100 frames one minute apart from 12:00Z: NY 08:00 (=13:00Z) is frame 60,
# while 08:00Z precedes the whole series.
_FRAMES_T0 = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=datetime.UTC)
_THIRTEEN_Z = datetime.datetime(2026, 1, 1, 13, 0, tzinfo=datetime.UTC)


def _seed_frames() -> int:
    with session_scope(get_context().session_factory) as db:
        cam = Camera(name="tz-cam", address="10.0.0.9", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(camera_id=cam.id, name="TZ Project", lifecycle_state="active")
        db.add(proj)
        db.flush()
        for i in range(100):
            t = _FRAMES_T0 + datetime.timedelta(minutes=i)
            db.add(
                Frame(
                    project_id=proj.id,
                    sequence_index=i,
                    capture_timestamp=t.replace(tzinfo=None),
                    file_path=f"/frames/{proj.id}/{i:08d}.jpg",
                    capture_status="captured",
                    origin="captured",
                    lifecycle_state="active",
                )
            )
        db.flush()
        return int(proj.id)


def _tile_epochs(html: str) -> list[int]:
    return [int(m) for m in re.findall(r'data-timestamp="(\d+)"', html)]


class TestFramesJumpInViewerZone:
    def test_naive_at_is_read_in_the_viewer_zone(
        self, admin_client: TestClient
    ) -> None:
        pid = _seed_frames()
        _set_zone("admin", _NY)
        html = admin_client.get(f"/frames?project_id={pid}&at=2026-01-01T08:00").text
        epochs = _tile_epochs(html)
        assert _epoch(_THIRTEEN_Z) in epochs
        # Newest-first: the window reaches past the anchor, and older frames too.
        assert max(epochs) > _epoch(_THIRTEEN_Z) > min(epochs)

    def test_offset_at_keeps_its_offset(self, admin_client: TestClient) -> None:
        pid = _seed_frames()
        _set_zone("admin", _NY)
        html = admin_client.get(
            f"/frames?project_id={pid}&at=2026-01-01T08:00%2B00:00"
        ).text
        epochs = _tile_epochs(html)
        # 08:00Z precedes the series: the window starts at the first frame.
        assert min(epochs) == _epoch(_FRAMES_T0)
        assert _epoch(_THIRTEEN_Z) not in epochs

    def test_utc_viewer_is_unchanged(self, admin_client: TestClient) -> None:
        pid = _seed_frames()
        html = admin_client.get(f"/frames?project_id={pid}&at=2026-01-01T13:00").text
        assert _epoch(_THIRTEEN_Z) in _tile_epochs(html)

    def test_jump_label_names_the_zone(self, admin_client: TestClient) -> None:
        pid = _seed_frames()
        _set_zone("admin", _NY)
        html = admin_client.get(f"/frames?project_id={pid}").text
        assert re.search(r"Jump to date &amp; time \(America/New_York\)", html)
        assert "Times are in America/New_York." in html


def _seed_events() -> None:
    base = datetime.datetime(2026, 1, 1, 12, 0)
    with session_scope(get_context().session_factory) as db:
        for i in range(120):
            db.add(
                Event(
                    scope="camera",
                    scope_id=1,
                    level="info",
                    message=f"tz{i}",
                    timestamp=base + datetime.timedelta(minutes=i),
                    event_metadata=None,
                )
            )


def _event_messages(html: str) -> list[str]:
    return re.findall(r">(tz\d+)<", html)


class TestEventsJumpInViewerZone:
    def test_naive_at_is_read_in_the_viewer_zone(
        self, admin_client: TestClient
    ) -> None:
        _seed_events()
        _set_zone("admin", _NY)
        html = admin_client.get("/events?scope=camera&at=2026-01-01T08:00").text
        assert _event_messages(html)[0] == "tz60"

    def test_offset_at_keeps_its_offset(self, admin_client: TestClient) -> None:
        _seed_events()
        _set_zone("admin", _NY)
        html = admin_client.get("/events?scope=camera&at=2026-01-01T12:30%2B00:00").text
        assert _event_messages(html)[0] == "tz30"

    def test_header_and_jump_label_name_the_zone(
        self, admin_client: TestClient
    ) -> None:
        _set_zone("admin", _NY)
        html = admin_client.get("/events").text
        assert re.search(r"<th>Time \(America/New_York\)</th>", html)
        assert re.search(r"Jump to date &amp; time \(America/New_York\)", html)

    def test_utc_viewer_header(self, admin_client: TestClient) -> None:
        html = admin_client.get("/events").text
        assert "<th>Time (UTC)</th>" in html

    def test_audit_header_names_the_zone(self, admin_client: TestClient) -> None:
        _set_zone("admin", _NY)
        html = admin_client.get("/events/audit").text
        assert re.search(r"<th>Time \(America/New_York\)</th>", html)


def _milestone_project() -> int:
    with session_scope(get_context().session_factory) as db:
        cam = Camera(name="ms-tz-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name="ms-tz",
            capture_interval_seconds=60,
            lifecycle_state="active",
            operational_status="idle",
            storage_path="/tmp/ms-tz",
        )
        db.add(proj)
        db.flush()
        return int(proj.id)


def _only_milestone(project_id: int) -> tuple[int, datetime.datetime | None]:
    with session_scope(get_context().session_factory) as db:
        ms = db.query(Milestone).filter(Milestone.project_id == project_id).one()
        return int(ms.id), ms.position_timestamp


class TestMilestoneInViewerZone:
    def _create(self, client: TestClient, pid: int, when: str) -> None:
        csrf = csrf_of(client, "/")
        resp = client.post(
            f"/projects/{pid}/milestones",
            data={
                "milestone_label": "Pour",
                "milestone_position_type": "time",
                "milestone_timestamp": when,
                "csrf_token": csrf,
            },
            headers=_FORM,
        )
        assert resp.status_code == 200

    def test_stored_as_naive_utc_and_edited_in_viewer_zone(
        self, operator_client: TestClient
    ) -> None:
        pid = _milestone_project()
        _set_zone("operator", _NY)
        self._create(operator_client, pid, "2026-01-01T08:00")
        ms_id, stored = _only_milestone(pid)
        assert stored == datetime.datetime(2026, 1, 1, 13, 0)
        form = operator_client.get(f"/projects/{pid}/milestones/{ms_id}/edit").text
        assert 'value="2026-01-01T08:00"' in form
        # The edit form's value is a January date: its label says EST, in any month.
        assert "Date &amp; time (EST)" in form

    def test_utc_viewer_is_unchanged(self, operator_client: TestClient) -> None:
        pid = _milestone_project()
        self._create(operator_client, pid, "2026-01-01T08:00")
        ms_id, stored = _only_milestone(pid)
        assert stored == datetime.datetime(2026, 1, 1, 8, 0)
        form = operator_client.get(f"/projects/{pid}/milestones/{ms_id}/edit").text
        assert 'value="2026-01-01T08:00"' in form
        assert "Date &amp; time (UTC)" in form

    def test_offset_input_is_stored_as_naive_utc(
        self, operator_client: TestClient
    ) -> None:
        pid = _milestone_project()
        _set_zone("operator", _NY)
        self._create(operator_client, pid, "2026-01-01T08:00+01:00")
        _, stored = _only_milestone(pid)
        assert stored == datetime.datetime(2026, 1, 1, 7, 0)


class TestFrameDrawerTimestampInViewerZone:
    """#124: the drawer's capture-timestamp edit reads and shows the viewer's zone."""

    def _frame(self) -> tuple[int, int]:
        pid = _seed_frames()
        with session_scope(get_context().session_factory) as db:
            frame = db.query(Frame).filter(Frame.project_id == pid).first()
            assert frame is not None
            return pid, int(frame.id)

    def _stored(self, frame_id: int) -> datetime.datetime:
        with session_scope(get_context().session_factory) as db:
            frame = db.get(Frame, frame_id)
            assert frame is not None
            return frame.capture_timestamp

    def _patch(
        self, client: TestClient, pid: int, fid: int, when: str
    ) -> httpx.Response:
        return client.patch(  # type: ignore[return-value]
            f"/projects/{pid}/frames/{fid}?drawer=1",
            data={"capture_timestamp": when, "csrf_token": csrf_of(client, "/")},
            headers=_FORM,
        )

    def test_typed_time_is_read_in_the_viewer_zone(
        self, admin_client: TestClient
    ) -> None:
        pid, fid = self._frame()
        _set_zone("admin", _NY)
        resp = self._patch(admin_client, pid, fid, "2026-01-01T08:00")
        assert resp.status_code == 200
        assert self._stored(fid) == datetime.datetime(2026, 1, 1, 13, 0)
        # The edit input shows the same wall time, and names the zone's EST.
        assert 'value="2026-01-01T08:00"' in resp.text
        assert "(EST)" in resp.text

    def test_utc_viewer_is_unchanged(self, admin_client: TestClient) -> None:
        pid, fid = self._frame()
        resp = self._patch(admin_client, pid, fid, "2026-01-01T08:00")
        assert resp.status_code == 200
        assert self._stored(fid) == datetime.datetime(2026, 1, 1, 8, 0)
        assert 'value="2026-01-01T08:00"' in resp.text

    def test_offset_value_keeps_its_offset(self, admin_client: TestClient) -> None:
        pid, fid = self._frame()
        _set_zone("admin", _NY)
        self._patch(admin_client, pid, fid, "2026-01-01T08:00+01:00")
        assert self._stored(fid) == datetime.datetime(2026, 1, 1, 7, 0)


# ---------------------------------------------------------------------------
# Status banner: the latest error's time in the viewer's zone (#319)
# ---------------------------------------------------------------------------


def _seed_banner_error() -> None:
    """One error event at a fixed January instant (11:23 UTC, 06:23 EST)."""
    with session_scope(get_context().session_factory) as db:
        db.add(
            Event(
                scope="system",
                scope_id=None,
                level="error",
                message="banner-tz-probe",
                timestamp=datetime.datetime(2026, 1, 15, 11, 23),
                event_metadata=None,
            )
        )


class TestStatusBannerInViewerZone:
    def test_banner_shows_the_local_time_not_utc(
        self, admin_client: TestClient
    ) -> None:
        _seed_banner_error()
        _set_zone("admin", _NY)
        html = admin_client.get("/partials/status").text
        assert "banner-tz-probe" in html
        assert "2026-01-15 06:23 EST" in html
        assert "11:23 UTC" not in html

    @pytest.mark.parametrize("zone", [None, "Not/A_Zone"])
    def test_no_or_invalid_zone_falls_back_to_utc(
        self, admin_client: TestClient, zone: str | None
    ) -> None:
        _seed_banner_error()
        _set_zone("admin", zone)
        html = admin_client.get("/partials/status").text
        assert "2026-01-15 11:23 UTC" in html
