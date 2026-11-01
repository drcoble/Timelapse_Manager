"""Web tests for the one-time "Read with admin account" geolocation lookup (#68).

``POST /cameras/{id}/geolocation/admin-read`` reads an Axis camera's position
once with an administrator login that must never be persisted, logged, echoed
or audited. These tests drive the real :class:`VapixAdapter` with only its HTTP
helper (``timelapse_manager.cameras.vapix.http_get_image``) patched, so the URL
scheme, the credentials actually sent, and the logging all run for real.

Covers:
- success renders the poll-result fragment with the coordinates;
- the typed login reaches the request and nowhere else (response, logs, audit
  events, the camera row);
- an ``https://`` address is read over HTTPS;
- a refused login, a missing username, a non-VAPIX camera;
- SSRF rejection before any probe; viewer 403; missing CSRF 403;
- the camera edit form ignores the geo_admin_* fields;
- ambiguous addresses (``?``, ``#``, ``\\``, userinfo) are refused before any
  resolve, reserve or probe;
- rate limiting: the in-memory limiter keyed on the resolved device IP, the
  camera and the user; the reservation made before the probe; release only
  when get.cgi accepted the login; and full isolation from the login throttle
  in both directions;
- one admin read per camera at a time (``admin_busy``, no reserve or probe),
  released on every return path; the fragment's client guards; CSRF delivered
  only in the ``X-CSRF-Token`` header (#70 WP-70-D).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from tests.conftest import csrf_of
from timelapse_manager.cameras.base import AuthCaptureError, OtherCaptureError
from timelapse_manager.db.models import Camera, Event, LoginAttempt
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.crypto import decrypt_credentials, encrypt_credentials
from timelapse_manager.security.throttle import BruteForceThrottle
from timelapse_manager.web.routers import cameras as cameras_router
from timelapse_manager.web.routers.cameras import GEO_ADMIN_MAX_ATTEMPTS

_ALLOWED_ADDRESS = "8.8.8.8"
_HTTP_GET = "timelapse_manager.cameras.vapix.http_get_image"

# Distinctive strings so any leak is unambiguous.
_ADMIN_USER = "adm-user-5b7e"
_ADMIN_PASS = "adm-pass-c41d"

_POSITION_XML = (
    b'<PositionResponse SchemaVersion="1.0"><Success><GetSuccess>'
    b"<Location><Lat>34.122748</Lat><Lng>-83.936878</Lng></Location>"
    b"<ValidPosition>true</ValidPosition>"
    b"</GetSuccess></Success></PositionResponse>"
)

_NO_POSITION_XML = (
    b"<PositionResponse><Success><GetSuccess><Location>"
    b"<Lat>0</Lat><Lng>0</Lng></Location>"
    b"<ValidPosition>false</ValidPosition>"
    b"</GetSuccess></Success></PositionResponse>"
)


def _seed_camera(
    *,
    name: str,
    address: str = _ALLOWED_ADDRESS,
    protocol: str = "vapix",
    credentials: dict[str, object] | None = None,
) -> int:
    """Insert a camera (optionally with encrypted credentials) and return its id."""
    with session_scope(get_context().session_factory) as db:
        cam = Camera(
            name=name,
            address=address,
            protocol=protocol,
            credentials=encrypt_credentials(credentials),
        )
        db.add(cam)
        db.flush()
        return cam.id


def _camera_row(camera_id: int) -> tuple[dict | None, float | None, str | None]:
    """Return (decrypted credentials, latitude, geo source) for a camera."""
    with session_scope(get_context().session_factory) as db:
        cam = db.get(Camera, camera_id)
        assert cam is not None
        return (
            decrypt_credentials(cam.credentials) if cam.credentials else None,
            cam.geolocation_latitude,
            cam.geolocation_source,
        )


def _limiter_count(key: tuple[str, str | int]) -> int:
    """Live admin-read reservations held against one limiter key."""
    return cameras_router._geo_admin_limiter.count(key)  # noqa: SLF001


def _login_rows() -> list[str]:
    """Usernames recorded by the login throttle."""
    with session_scope(get_context().session_factory) as db:
        return [r.username for r in db.execute(select(LoginAttempt)).scalars()]


def _camera_events(camera_id: int) -> list[str]:
    with session_scope(get_context().session_factory) as db:
        rows = (
            db.execute(
                select(Event).where(
                    Event.scope == "camera", Event.scope_id == camera_id
                )
            )
            .scalars()
            .all()
        )
        return [r.message for r in rows]


def _admin_read(
    client: TestClient,
    camera_id: int,
    *,
    username: str = _ADMIN_USER,
    password: str = _ADMIN_PASS,
    headers: dict[str, str] | None = None,
):
    """POST the admin-read form the way the fragment's button does."""
    csrf = csrf_of(client, "/cameras")
    return client.post(
        f"/cameras/{camera_id}/geolocation/admin-read",
        data={
            "geo_admin_username": username,
            "geo_admin_password": password,
            "csrf_token": csrf,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            **(headers or {}),
        },
    )


def _resolving_to(mapping: dict[str, str]):
    """Patch the SSRF resolver so each named host resolves to one public IP."""
    import ipaddress

    async def _resolve(host: str, **_kwargs: object) -> list[object]:
        return [ipaddress.ip_address(mapping.get(host, host))]

    return patch(
        "timelapse_manager.security.ssrf.resolve_and_check_async", new=_resolve
    )


_REFUSAL = AuthCaptureError("authentication rejected (401) for x")


class TestAdminReadSuccess:
    def test_success_renders_coordinates_without_echoing_login(
        self, admin_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        camera_id = _seed_camera(
            name="adm-ok",
            credentials={"username": "timelapse", "password": "op-pass"},
        )
        mock_get = AsyncMock(return_value=_POSITION_XML)
        with caplog.at_level(logging.DEBUG), patch(_HTTP_GET, new=mock_get):
            resp = _admin_read(admin_client, camera_id)

        assert resp.status_code == 200, resp.text
        html = resp.text
        assert "Camera reports:" in html
        assert "34.122748" in html
        assert "Use these values" in html
        # The typed login went to the camera, and only get.cgi was read.
        assert mock_get.await_count == 1
        url, credentials = mock_get.await_args.args[1], mock_get.await_args.args[2]
        assert url == f"http://{_ALLOWED_ADDRESS}/axis-cgi/geolocation/get.cgi"
        assert credentials == (_ADMIN_USER, _ADMIN_PASS)
        # ...and nowhere else: response, logs, audit, the camera row.
        for text in [html, *(r.getMessage() for r in caplog.records)]:
            assert _ADMIN_USER not in text
            assert _ADMIN_PASS not in text
        events = _camera_events(camera_id)
        assert any("one-time administrator account" in m for m in events)
        assert all(_ADMIN_USER not in m and _ADMIN_PASS not in m for m in events)
        creds, latitude, source = _camera_row(camera_id)
        assert creds == {"username": "timelapse", "password": "op-pass"}
        assert latitude is None
        assert source is None
        # Nothing about the typed login reaches the login throttle either.
        assert all(_ADMIN_USER not in name for name in _login_rows())

    def test_https_address_is_read_over_https(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="adm-https", address="https://8.8.8.8")
        mock_get = AsyncMock(return_value=_POSITION_XML)
        with patch(_HTTP_GET, new=mock_get):
            resp = _admin_read(admin_client, camera_id)
        assert resp.status_code == 200
        assert mock_get.await_args.args[1].startswith("https://8.8.8.8/")

    def test_operator_may_use_it(self, operator_client: TestClient) -> None:
        camera_id = _seed_camera(name="adm-operator")
        with patch(_HTTP_GET, new=AsyncMock(return_value=_POSITION_XML)):
            resp = _admin_read(operator_client, camera_id)
        assert resp.status_code == 200
        assert "Camera reports:" in resp.text


class TestAdminReadFailures:
    def test_refused_login_renders_warning_and_form_again(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="adm-refused")
        refusal = AuthCaptureError(
            "authentication rejected (401) for http://8.8.8.8/axis-cgi/x"
        )
        with patch(_HTTP_GET, new=AsyncMock(side_effect=refusal)):
            resp = _admin_read(admin_client, camera_id)
        assert resp.status_code == 200
        html = resp.text
        assert "alert warning" in html
        assert "refused the administrator login" in html
        assert "Administrator" in html
        assert 'name="geo_admin_password"' in html
        assert _ADMIN_USER not in html
        assert _ADMIN_PASS not in html

    def test_missing_username_does_not_probe(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="adm-missing")
        mock_get = AsyncMock(return_value=_POSITION_XML)
        with patch(_HTTP_GET, new=mock_get):
            resp = _admin_read(admin_client, camera_id, username="  ")
        assert resp.status_code == 200
        assert "Enter the administrator username" in resp.text
        mock_get.assert_not_awaited()

    def test_non_vapix_camera_is_not_probed(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="adm-onvif", protocol="onvif")
        mock_get = AsyncMock(return_value=_POSITION_XML)
        with patch(_HTTP_GET, new=mock_get):
            resp = _admin_read(admin_client, camera_id)
        assert resp.status_code == 200
        assert "VAPIX (Axis) cameras only" in resp.text
        mock_get.assert_not_awaited()

    def test_ssrf_rejected_address_is_unreachable_without_probe(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="adm-ssrf", address="127.0.0.1")
        mock_get = AsyncMock(return_value=_POSITION_XML)
        with patch(_HTTP_GET, new=mock_get):
            resp = _admin_read(admin_client, camera_id)
        assert resp.status_code == 200
        assert "Could not reach this camera" in resp.text
        mock_get.assert_not_awaited()
        assert _limiter_count(("camera", camera_id)) == 0

    @pytest.mark.parametrize(
        "address",
        [
            "8.8.8.8?x=@10.0.0.1",
            "8.8.8.8#@10.0.0.1",
            "8.8.8.8\\@10.0.0.1",
            "user:pw@8.8.8.8",
            "http://user:pw@8.8.8.8",
            "evil@10.0.0.1@8.8.8.8",
        ],
    )
    def test_ambiguous_address_is_refused_before_anything(
        self, admin_client: TestClient, address: str
    ) -> None:
        camera_id = _seed_camera(
            name=f"adm-ambiguous-{abs(hash(address))}", address=address
        )
        mock_get = AsyncMock(return_value=_POSITION_XML)
        with patch(_HTTP_GET, new=mock_get):
            resp = _admin_read(admin_client, camera_id)
        assert resp.status_code == 200
        assert "Could not reach this camera" in resp.text
        mock_get.assert_not_awaited()
        assert _limiter_count(("camera", camera_id)) == 0

    def test_unknown_camera_is_404(self, admin_client: TestClient) -> None:
        resp = _admin_read(admin_client, 999_999)
        assert resp.status_code == 404

    def test_viewer_is_forbidden(self, viewer_client: TestClient) -> None:
        camera_id = _seed_camera(name="adm-viewer")
        mock_get = AsyncMock(return_value=_POSITION_XML)
        with patch(_HTTP_GET, new=mock_get):
            resp = _admin_read(viewer_client, camera_id)
        assert resp.status_code == 403
        mock_get.assert_not_awaited()

    def test_missing_csrf_is_forbidden(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="adm-csrf")
        mock_get = AsyncMock(return_value=_POSITION_XML)
        with patch(_HTTP_GET, new=mock_get):
            resp = admin_client.post(
                f"/cameras/{camera_id}/geolocation/admin-read",
                data={
                    "geo_admin_username": _ADMIN_USER,
                    "geo_admin_password": _ADMIN_PASS,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 403
        mock_get.assert_not_awaited()


class TestEditFormIgnoresAdminFields:
    def test_geo_admin_fields_never_become_camera_credentials(
        self, admin_client: TestClient
    ) -> None:
        """Even if a browser submitted them with Save, they are not the creds."""
        camera_id = _seed_camera(
            name="adm-edit",
            credentials={"username": "timelapse", "password": "op-pass"},
        )
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "adm-edit",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "username": "timelapse",
                "password": "",
                "geo_admin_username": _ADMIN_USER,
                "geo_admin_password": _ADMIN_PASS,
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code == 200, resp.text
        creds, _, _ = _camera_row(camera_id)
        assert creds == {"username": "timelapse", "password": "op-pass"}


class TestAdminReadRateLimit:
    def test_refusals_exhaust_the_budget(self, admin_client: TestClient) -> None:
        """After the attempt budget, the next read is refused without a probe."""
        camera_id = _seed_camera(name="adm-cap")
        mock_get = AsyncMock(side_effect=_REFUSAL)
        with patch(_HTTP_GET, new=mock_get):
            for _ in range(GEO_ADMIN_MAX_ATTEMPTS):
                resp = _admin_read(admin_client, camera_id)
                assert "refused the administrator login" in resp.text
            resp = _admin_read(admin_client, camera_id)
        assert "Too many refused administrator logins" in resp.text
        assert mock_get.await_count == GEO_ADMIN_MAX_ATTEMPTS
        assert any("admin_throttled" in m for m in _camera_events(camera_id))

    def test_device_budget_follows_the_resolved_ip(
        self, admin_client: TestClient
    ) -> None:
        """Case, trailing-dot and alias spellings of one device share a budget."""
        first = _seed_camera(name="adm-alias-1", address="cam-a.example")
        second = _seed_camera(name="adm-alias-2", address="http://CAM-B.example.:80")
        resolver = _resolving_to(
            {"cam-a.example": "8.8.4.4", "cam-b.example.": "8.8.4.4"}
        )
        with resolver, patch(_HTTP_GET, new=AsyncMock(side_effect=_REFUSAL)):
            for _ in range(GEO_ADMIN_MAX_ATTEMPTS - 1):
                _admin_read(admin_client, first)
            _admin_read(admin_client, second)
        assert _limiter_count(("device", "8.8.4.4")) == GEO_ADMIN_MAX_ATTEMPTS
        # The second row has its own camera budget, but the device is spent.
        mock_get = AsyncMock(return_value=_POSITION_XML)
        with resolver, patch(_HTTP_GET, new=mock_get):
            resp = _admin_read(admin_client, second)
        assert "Too many refused administrator logins" in resp.text
        mock_get.assert_not_awaited()

    def test_user_budget_spans_cameras(self, admin_client: TestClient) -> None:
        """One operator cannot spread guesses over many devices."""
        camera_ids = [
            _seed_camera(name=f"adm-user-{n}", address=f"8.8.4.{n + 1}")
            for n in range(GEO_ADMIN_MAX_ATTEMPTS + 1)
        ]
        with patch(_HTTP_GET, new=AsyncMock(side_effect=_REFUSAL)):
            for camera_id in camera_ids[:-1]:
                _admin_read(admin_client, camera_id)
        mock_get = AsyncMock(return_value=_POSITION_XML)
        with patch(_HTTP_GET, new=mock_get):
            resp = _admin_read(admin_client, camera_ids[-1])
        assert "Too many refused administrator logins" in resp.text
        mock_get.assert_not_awaited()

    def test_attempt_is_counted_on_every_key(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="adm-keys")
        with patch(_HTTP_GET, new=AsyncMock(side_effect=_REFUSAL)):
            _admin_read(admin_client, camera_id)
        assert _limiter_count(("camera", camera_id)) == 1
        assert _limiter_count(("device", _ALLOWED_ADDRESS)) == 1

    def test_attempt_is_reserved_before_the_probe(
        self, admin_client: TestClient
    ) -> None:
        """The attempt is already counted while the camera is being asked."""
        camera_id = _seed_camera(name="adm-reserve")
        seen_during_probe: list[int] = []

        async def _probe(*_args: object, **_kwargs: object) -> bytes:
            seen_during_probe.append(_limiter_count(("camera", camera_id)))
            return _POSITION_XML

        with patch(_HTTP_GET, new=AsyncMock(side_effect=_probe)):
            resp = _admin_read(admin_client, camera_id)
        assert "Camera reports:" in resp.text
        assert seen_during_probe == [1]
        # The accepted login then releases it.
        assert _limiter_count(("camera", camera_id)) == 0

    def test_accepted_login_releases_only_its_own_attempt(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="adm-release")
        with patch(_HTTP_GET, new=AsyncMock(side_effect=_REFUSAL)):
            _admin_read(admin_client, camera_id)
        # A 2xx from get.cgi with no position set still proves the login.
        with patch(_HTTP_GET, new=AsyncMock(return_value=_NO_POSITION_XML)):
            resp = _admin_read(admin_client, camera_id)
        assert "has no location" in resp.text
        assert _limiter_count(("camera", camera_id)) == 1

    def test_non_2xx_outcomes_are_never_released(
        self, admin_client: TestClient
    ) -> None:
        """Only get.cgi accepting the login releases (no legacy fallback)."""
        camera_id = _seed_camera(name="adm-no-release")
        mock_get = AsyncMock(side_effect=OtherCaptureError("unexpected status 404"))
        with patch(_HTTP_GET, new=mock_get):
            for _ in range(GEO_ADMIN_MAX_ATTEMPTS):
                resp = _admin_read(admin_client, camera_id)
                assert "Could not reach this camera" in resp.text
            resp = _admin_read(admin_client, camera_id)
        assert "Too many refused administrator logins" in resp.text
        # Exactly one get.cgi request per attempt: no legacy param.cgi read.
        assert mock_get.await_count == GEO_ADMIN_MAX_ATTEMPTS


class TestIsolationFromLoginThrottle:
    def test_login_failures_never_touch_the_admin_read_budget(
        self, admin_client: TestClient
    ) -> None:
        """POST /login with crafted usernames cannot lock a camera's admin read."""
        camera_id = _seed_camera(name="adm-login-iso")
        crafted = [
            f"camera-geo-admin:{_ALLOWED_ADDRESS}",
            f"device:{_ALLOWED_ADDRESS}",
            f"camera:{camera_id}",
        ]
        for _ in range(GEO_ADMIN_MAX_ATTEMPTS + 1):
            for name in crafted:
                admin_client.post(
                    "/login",
                    data={"username": name, "password": "wrong"},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    follow_redirects=False,
                )
        assert _limiter_count(("camera", camera_id)) == 0
        assert _limiter_count(("device", _ALLOWED_ADDRESS)) == 0
        with patch(_HTTP_GET, new=AsyncMock(return_value=_POSITION_XML)):
            resp = _admin_read(admin_client, camera_id)
        assert "Camera reports:" in resp.text

    def test_admin_read_failures_never_touch_the_login_throttle(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="adm-read-iso")
        rows_before = _login_rows()
        with patch(_HTTP_GET, new=AsyncMock(side_effect=_REFUSAL)):
            for _ in range(GEO_ADMIN_MAX_ATTEMPTS + 1):
                _admin_read(admin_client, camera_id)
        assert _login_rows() == rows_before
        throttle = BruteForceThrottle(get_context().settings.auth)
        assert throttle.is_throttled(ip="testclient", username="admin") is False


# ---------------------------------------------------------------------------
# #70 WP-70-D: one admin read per camera at a time, and the fragment's guards.
# ---------------------------------------------------------------------------

_PARK_SECONDS = 10.0


@pytest.fixture(autouse=True)
def _clear_admin_inflight() -> Iterator[None]:
    """Start and end every test with no admin read marked in flight."""
    cameras_router._geo_admin_inflight.clear()  # noqa: SLF001
    yield
    cameras_router._geo_admin_inflight.clear()  # noqa: SLF001


@contextmanager
def _in_background(send: Callable[[], Any]) -> Iterator[dict[str, Any]]:
    """Run ``send`` on a worker thread; yield a dict that receives its result."""
    outcome: dict[str, Any] = {}

    def run() -> None:
        try:
            outcome["response"] = send()
        except BaseException as exc:  # noqa: BLE001 -- re-raised below
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        yield outcome
    finally:
        worker.join(timeout=_PARK_SECONDS + 10)
    if "error" in outcome:
        raise outcome["error"]


class TestAdminReadInFlight:
    def test_second_read_while_one_runs_is_busy(
        self, admin_client: TestClient, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A double click or a second tab gets ``admin_busy``: no reserve, no probe."""
        camera_id = _seed_camera(name="adm-busy")
        parked = threading.Event()
        release = threading.Event()

        async def _parked_probe(*_args: object, **_kwargs: object) -> bytes:
            parked.set()
            # Wait off the loop, so the second request is served meanwhile.
            await asyncio.to_thread(release.wait, _PARK_SECONDS)
            return _POSITION_XML

        mock_get = AsyncMock(side_effect=_parked_probe)
        with (
            caplog.at_level(logging.DEBUG),
            patch(_HTTP_GET, new=mock_get),
            _in_background(lambda: _admin_read(admin_client, camera_id)) as first,
        ):
            try:
                assert parked.wait(_PARK_SECONDS), "the first read never probed"
                second = _admin_read(admin_client, camera_id)
                # The first read holds the camera's only reservation.
                assert _limiter_count(("camera", camera_id)) == 1
            finally:
                release.set()

        assert second.status_code == 200, second.text
        assert "already running" in second.text
        assert "Camera reports:" not in second.text
        assert mock_get.await_count == 1
        first_resp = first["response"]
        assert first_resp.status_code == 200, first_resp.text
        assert "Camera reports:" in first_resp.text
        # The accepted login released the one reservation; nothing is left over.
        assert _limiter_count(("camera", camera_id)) == 0
        assert not cameras_router._geo_admin_inflight  # noqa: SLF001
        for text in [
            first_resp.text,
            second.text,
            *(r.getMessage() for r in caplog.records),
        ]:
            assert _ADMIN_PASS not in text
            assert _ADMIN_USER not in text
        assert any("outcome: admin_busy" in m for m in _camera_events(camera_id))

    def test_busy_fragment_offers_the_form_again(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="adm-busy-form")
        cameras_router._geo_admin_inflight.add(camera_id)  # noqa: SLF001
        with patch(_HTTP_GET, new=AsyncMock(return_value=_POSITION_XML)) as mock_get:
            resp = _admin_read(admin_client, camera_id)
        assert "already running" in resp.text
        assert 'name="geo_admin_password"' in resp.text
        assert mock_get.await_count == 0
        assert _limiter_count(("camera", camera_id)) == 0

    @pytest.mark.parametrize(
        ("protocol", "username", "address"),
        [
            ("onvif", _ADMIN_USER, _ALLOWED_ADDRESS),  # admin_unsupported
            ("vapix", "", _ALLOWED_ADDRESS),  # admin_missing
            ("vapix", _ADMIN_USER, "bad?address"),  # unreachable (refused address)
        ],
    )
    def test_early_returns_do_not_leave_the_camera_busy(
        self, admin_client: TestClient, protocol: str, username: str, address: str
    ) -> None:
        camera_id = _seed_camera(name="adm-early", protocol=protocol, address=address)
        with patch(_HTTP_GET, new=AsyncMock(return_value=_POSITION_XML)):
            first = _admin_read(admin_client, camera_id, username=username)
            second = _admin_read(admin_client, camera_id, username=username)
        assert "already running" not in first.text
        assert "already running" not in second.text
        assert not cameras_router._geo_admin_inflight  # noqa: SLF001

    def test_failed_probe_does_not_leave_the_camera_busy(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="adm-fail-free")
        with patch(_HTTP_GET, new=AsyncMock(side_effect=_REFUSAL)):
            _admin_read(admin_client, camera_id)
        assert not cameras_router._geo_admin_inflight  # noqa: SLF001


class TestAdminReadFragmentGuards:
    def test_button_is_guarded_and_clears_the_password(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="adm-guards")
        resp = _admin_read(admin_client, camera_id, username="")
        assert "Enter the administrator username" in resp.text
        aid = f"cam-{camera_id}-geo-admin"
        assert 'hx-disabled-elt="this"' in resp.text
        assert 'hx-sync="this:drop"' in resp.text
        assert f'data-clear-on-done="#{aid}-password"' in resp.text
        assert f'hx-indicator="#{aid}-spinner"' in resp.text
        assert f'id="{aid}-password"' in resp.text

    def test_csrf_in_the_header_only_is_accepted(
        self, admin_client: TestClient
    ) -> None:
        """The button sends no csrf_token field (hx-params); the header carries it."""
        camera_id = _seed_camera(name="adm-header-csrf")
        token = csrf_of(admin_client, "/cameras")
        with patch(_HTTP_GET, new=AsyncMock(return_value=_POSITION_XML)):
            resp = admin_client.post(
                f"/cameras/{camera_id}/geolocation/admin-read",
                data={
                    "geo_admin_username": _ADMIN_USER,
                    "geo_admin_password": _ADMIN_PASS,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "X-CSRF-Token": token,
                    "HX-Request": "true",
                },
            )
        assert resp.status_code == 200, resp.text
        assert "Camera reports:" in resp.text
