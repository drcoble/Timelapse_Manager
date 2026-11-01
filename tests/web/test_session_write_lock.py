"""The LDAP session re-check never holds SQLite's write lock across a request (#66 C1).

A directory-backed session is re-checked against the directory once its
interval has elapsed. The re-check writes (``last_revalidated_at``, a changed
role, or a revoke). Before #66 those writes were flushed into the *request's*
transaction, so on an ``async`` GET that then awaited a camera probe the request
held the write lock for the whole probe (seconds, up to the busy timeout), and
every other writer -- the capture path included -- stalled or failed behind it.

These tests drive real requests through the app with the camera adapter faked
so the probe *parks* until the test releases it. While it is parked, a second
raw ``sqlite3`` connection with ``timeout=0`` must be able to ``BEGIN
IMMEDIATE``: that fails at once if anyone still holds the write lock.

They also pin the revoke defect found while verifying C1: a revoke decided on a
GET used to be rolled back with the 401, so it was never persisted and every
later request asked the directory again.

The remaining checks cover the companion fixes in the same WP:

* C11 -- a parked probe holds no pooled connection;
* N2 -- the ``_shared.py`` probe sites resolve the camera host off the loop.
"""

from __future__ import annotations

import asyncio
import datetime
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

import timelapse_manager.security.session_revalidation as revalidation
import timelapse_manager.security.ssrf as ssrf_mod
import timelapse_manager.web.routers.alerts as alerts_router
from tests.conftest import csrf_of
from tests.web.test_login_ldap import (
    _ADMIN_GROUP,
    _LDAP_USER,
    _backdate_revalidation,
    _dir_state,
    _login_ldap_user,
)
from timelapse_manager.cameras.base import (
    GeoLocation,
    PTZPresetsResult,
    StreamProfileResult,
)
from timelapse_manager.db.models import Camera, Project, User
from timelapse_manager.db.models import Session as SessionRow
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.ldap_directory import LdapOutcome

# A public IP literal: the SSRF guard passes it without a DNS lookup.
_CAMERA_ADDRESS = "8.8.8.8"

# How long a parked probe waits for the test before giving up. Well under the
# 4 s probe ceiling in ``_shared.py``, so a parked probe never times out first.
_PARK_SECONDS = 3.0


# ---------------------------------------------------------------------------
# Seeding and inspection
# ---------------------------------------------------------------------------


def _db_path() -> str:
    """Return the file path of the running app's SQLite database."""
    url = get_context().settings.database.url
    return url.removeprefix("sqlite:///")


def _seed_camera_and_project() -> tuple[int, int]:
    """Insert a VAPIX camera and an anchor-free project on it; return the ids."""
    with session_scope(get_context().session_factory) as db:
        camera = Camera(
            name="lock-cam",
            address=_CAMERA_ADDRESS,
            protocol="vapix",
            snapshot_uri=f"http://{_CAMERA_ADDRESS}/snap",
        )
        db.add(camera)
        db.flush()
        project = Project(
            camera_id=camera.id,
            name="lock-project",
            capture_interval_seconds=60,
            lifecycle_state="active",
        )
        db.add(project)
        db.flush()
        return camera.id, project.id


def _ldap_session() -> SessionRow:
    """Return the LDAP user's newest session row (revoked or not), detached."""
    with session_scope(get_context().session_factory) as db:
        user = db.query(User).filter(User.username == _LDAP_USER).one()
        return (
            db.query(SessionRow)
            .filter(SessionRow.user_id == user.id)
            .order_by(SessionRow.id.desc())
            .first()
        )


def _write_lock_is_free() -> bool:
    """Return whether another connection can take the write lock right now.

    ``timeout=0`` makes ``BEGIN IMMEDIATE`` fail at once with "database is
    locked" when any connection holds the RESERVED lock.
    """
    raw = sqlite3.connect(_db_path(), timeout=0)
    try:
        raw.execute("BEGIN IMMEDIATE")
        raw.execute("ROLLBACK")
        return True
    except sqlite3.OperationalError as exc:
        if "database is locked" not in str(exc):
            raise
        return False
    finally:
        raw.close()


def _counting_resolver(
    monkeypatch: pytest.MonkeyPatch, outcome: LdapOutcome, groups: frozenset[str]
) -> list[str]:
    """Patch the directory to answer ``outcome``; return the list of lookups."""
    calls: list[str] = []

    def resolve(**kwargs: Any) -> Any:
        calls.append(kwargs["username"])
        return _dir_state(outcome, groups)

    monkeypatch.setattr(revalidation, "resolve_directory_state", resolve)
    return calls


# ---------------------------------------------------------------------------
# A probe that parks until the test releases it
# ---------------------------------------------------------------------------


class _ParkedProbe:
    """Coordinates a fake camera call (on the app's loop) with the test thread.

    The fake awaits :meth:`park`, which signals ``parked`` and then waits, in a
    worker thread so the app's loop stays free, until the test sets
    ``release``. ``release`` is always set on exit so a failing assertion
    cannot leave the request (and the TestClient) hanging.
    """

    def __init__(self) -> None:
        self.parked = threading.Event()
        self.release = threading.Event()

    async def park(self) -> None:
        self.parked.set()
        await asyncio.to_thread(self.release.wait, _PARK_SECONDS)

    def adapter(self) -> MagicMock:
        """Return a fake adapter whose probe methods all park."""

        async def stream_profiles() -> StreamProfileResult:
            await self.park()
            return StreamProfileResult(profiles=[], ok=True)

        async def ptz_presets() -> PTZPresetsResult:
            await self.park()
            return PTZPresetsResult(presets=[], ptz_supported=False, ok=True)

        async def event_topics() -> list[Any]:
            await self.park()
            return []

        async def geolocation() -> GeoLocation | None:
            await self.park()
            return None

        adapter = MagicMock()
        adapter.list_stream_profiles = stream_profiles
        adapter.list_ptz_presets = ptz_presets
        adapter.list_event_topics = event_topics
        adapter.get_geolocation = geolocation
        adapter.close = AsyncMock()
        return adapter


@contextmanager
def _request_in_background(
    send: Callable[[], httpx.Response],
) -> Iterator[dict[str, Any]]:
    """Run ``send`` on a worker thread; yield a dict that receives the response."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["response"] = send()
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the test below
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        yield outcome
    finally:
        worker.join(timeout=_PARK_SECONDS + 10)
    if "error" in outcome:
        raise outcome["error"]


def _assert_lock_free_while_parked(
    client: TestClient, path: str, probe: _ParkedProbe
) -> httpx.Response:
    """GET ``path`` with the probe parked; assert the write lock is free meanwhile."""
    with _request_in_background(lambda: client.get(path)) as outcome:
        try:
            assert probe.parked.wait(_PARK_SECONDS), "the camera probe never started"
            assert _write_lock_is_free(), (
                f"GET {path} held the SQLite write lock across the camera probe"
            )
        finally:
            probe.release.set()
    response = outcome["response"]
    assert isinstance(response, httpx.Response)
    return response


@pytest.fixture()
def ldap_client(web_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A client signed in as a directory admin whose re-check is now due."""
    _login_ldap_user(web_client, monkeypatch, frozenset({_ADMIN_GROUP}))
    _backdate_revalidation(_LDAP_USER, minutes_ago=60)
    return web_client


# ---------------------------------------------------------------------------
# C1: the re-check commits before the route's camera await
# ---------------------------------------------------------------------------


class TestRecheckReleasesWriteLock:
    def test_settings_form_probe_does_not_hold_the_write_lock(
        self, ldap_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The audit's reproducer: an operator opens project settings after a while."""
        calls = _counting_resolver(
            monkeypatch, LdapOutcome.AUTHENTICATED, frozenset({_ADMIN_GROUP})
        )
        _, project_id = _seed_camera_and_project()
        before = _ldap_session().last_revalidated_at
        probe = _ParkedProbe()
        with patch(
            "timelapse_manager.cameras.build_adapter", return_value=probe.adapter()
        ):
            response = _assert_lock_free_while_parked(
                ldap_client, f"/projects/{project_id}/settings/form", probe
            )
        assert response.status_code == 200
        assert calls == [_LDAP_USER]
        after = _ldap_session().last_revalidated_at
        assert before is not None and after is not None and after > before

    def test_geolocation_probe_does_not_hold_the_write_lock(
        self, ldap_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The longest hold in the audit (two sequential 10 s camera requests)."""
        _counting_resolver(
            monkeypatch, LdapOutcome.AUTHENTICATED, frozenset({_ADMIN_GROUP})
        )
        camera_id, _ = _seed_camera_and_project()
        probe = _ParkedProbe()
        with patch(
            "timelapse_manager.cameras.build_adapter", return_value=probe.adapter()
        ):
            response = _assert_lock_free_while_parked(
                ldap_client, f"/cameras/{camera_id}/geolocation", probe
            )
        assert response.status_code == 200

    def test_sync_poller_handler_runs_without_the_write_lock(
        self, ldap_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The 30 s alerts poll is the request that most often takes the re-check."""
        _counting_resolver(
            monkeypatch, LdapOutcome.AUTHENTICATED, frozenset({_ADMIN_GROUP})
        )
        original = alerts_router._active_alerts_fragment
        lock_free: list[bool] = []

        def spy(*args: Any, **kwargs: Any) -> Any:
            lock_free.append(_write_lock_is_free())
            return original(*args, **kwargs)

        monkeypatch.setattr(alerts_router, "_active_alerts_fragment", spy)
        response = ldap_client.get("/alerts/summary")
        assert response.status_code == 200
        assert lock_free == [True]

    def test_unsafe_method_recheck_still_happens_once(
        self, ldap_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """POST is unchanged: the CSRF middleware re-checks, the dependency does not."""
        token = csrf_of(ldap_client, "/cameras")
        # The GET above took the due re-check against the unreachable test
        # directory, which opened the circuit breaker (#67 R3); close it so the
        # POST's re-check is asked again, then make it due.
        with session_scope(get_context().session_factory) as db:
            revalidation.breaker_for(db).record_answered()
        _backdate_revalidation(_LDAP_USER, minutes_ago=60)
        calls = _counting_resolver(
            monkeypatch, LdapOutcome.AUTHENTICATED, frozenset({_ADMIN_GROUP})
        )
        response = ldap_client.post(
            "/cameras/query",
            data={"address": ""},
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 200
        assert calls == [_LDAP_USER]
        last = _ldap_session().last_revalidated_at
        assert last is not None
        age = datetime.datetime.now(datetime.UTC).replace(tzinfo=None) - last
        assert age < datetime.timedelta(minutes=1)


# ---------------------------------------------------------------------------
# The revoke found while verifying C1
# ---------------------------------------------------------------------------


class TestRevokeOnGetIsPersisted:
    def test_revoke_decided_on_a_get_survives_the_401(
        self, ldap_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = _counting_resolver(monkeypatch, LdapOutcome.NO_SUCH_USER, frozenset())
        response = ldap_client.get("/alerts/summary")
        assert response.status_code == 401
        assert calls == [_LDAP_USER]
        row = _ldap_session()
        assert row.revoked is True
        assert row.revoked_at is not None

        # The stored revoke answers the next request; the directory is not asked.
        again = ldap_client.get("/alerts/summary")
        assert again.status_code == 401
        assert calls == [_LDAP_USER]


# ---------------------------------------------------------------------------
# C11: a parked probe holds no pooled connection
# ---------------------------------------------------------------------------


class TestProbeReleasesPooledConnection:
    def test_stream_profile_picker_returns_its_connection_before_the_probe(
        self, ldap_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _counting_resolver(
            monkeypatch, LdapOutcome.AUTHENTICATED, frozenset({_ADMIN_GROUP})
        )
        camera_id, _ = _seed_camera_and_project()
        pool = get_context().db_engine.pool
        baseline = pool.checkedout()  # type: ignore[attr-defined]
        probe = _ParkedProbe()
        path = f"/cameras/stream-profiles?camera_id={camera_id}"
        with (
            patch(
                "timelapse_manager.cameras.build_adapter", return_value=probe.adapter()
            ),
            _request_in_background(lambda: ldap_client.get(path)) as outcome,
        ):
            try:
                assert probe.parked.wait(_PARK_SECONDS)
                held = pool.checkedout()  # type: ignore[attr-defined]
            finally:
                probe.release.set()
        assert outcome["response"].status_code == 200
        assert held == baseline


# ---------------------------------------------------------------------------
# N2: the _shared.py probe sites resolve the camera host off the loop
# ---------------------------------------------------------------------------


class TestProbeHostResolutionOffLoop:
    def test_settings_form_resolves_the_camera_host_in_a_worker(
        self, ldap_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _counting_resolver(
            monkeypatch, LdapOutcome.AUTHENTICATED, frozenset({_ADMIN_GROUP})
        )
        _, project_id = _seed_camera_and_project()
        on_loop: list[bool] = []
        real = ssrf_mod.resolve_and_check

        def spy(host: str, **kwargs: Any) -> Any:
            try:
                asyncio.get_running_loop()
                on_loop.append(True)
            except RuntimeError:
                on_loop.append(False)
            return real(host, **kwargs)

        monkeypatch.setattr(ssrf_mod, "resolve_and_check", spy)
        adapter = _ParkedProbe().adapter()
        adapter.list_stream_profiles = AsyncMock(
            return_value=StreamProfileResult(profiles=[], ok=True)
        )
        adapter.list_ptz_presets = AsyncMock(
            return_value=PTZPresetsResult(presets=[], ptz_supported=False, ok=True)
        )
        adapter.list_event_topics = AsyncMock(return_value=[])
        with patch("timelapse_manager.cameras.build_adapter", return_value=adapter):
            response = ldap_client.get(f"/projects/{project_id}/settings/form")
        assert response.status_code == 200
        # One resolution per probe (stream profiles, PTZ presets, event topics),
        # each in a worker thread rather than on the event loop.
        assert on_loop == [False, False, False]
