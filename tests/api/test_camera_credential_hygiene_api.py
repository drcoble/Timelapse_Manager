"""The camera API keeps logins out of its output and the default on its LAN.

* ``POST /api/v1/cameras`` with ``user:pass@`` in a URI moves the login into the
  encrypted credential document, or answers ``422`` when the camera already has
  one (#69 CAM-S2); the response never shows the userinfo.
* ``capture-status`` redacts a stored ``last_error`` on the way out.
* ``detect-protocol`` and ``validate`` send the saved default login only to an
  address inside the allowed private subnets; discovery enrichment sends none
  (#67-S3, owner decision OD-1).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from timelapse_manager.cameras.base import DiscoveredCamera
from timelapse_manager.cameras.clients import allow_uncontained_double
from timelapse_manager.cameras.probing import DetectionOutcome
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.camera_defaults_service import (
    CameraDefaultsUpdate,
    update_settings,
)
from timelapse_manager.security.crypto import decrypt_credentials

API = "/api/v1/cameras"
_OUTSIDE = "93.184.216.34"
_INSIDE = "10.0.16.5"
_DEFAULT = ("fleet", "fleet-pw")
_SECRET = "hunter2-camera-pw"


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def client(migrated_client: TestClient) -> Iterator[TestClient]:
    with session_scope(get_context().session_factory) as db:
        update_settings(
            db,
            CameraDefaultsUpdate(
                enabled=True, username=_DEFAULT[0], password=_DEFAULT[1]
            ),
        )
    yield migrated_client


class TestCreate:
    def test_a_login_in_a_uri_is_moved_and_never_returned(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        resp = client.post(
            API,
            json={
                "name": "moved",
                "protocol": "http",
                "snapshot_uri": f"http://admin:{_SECRET}@{_INSIDE}/snap.jpg",
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 201, resp.text
        assert _SECRET not in resp.text
        assert resp.json()["snapshot_uri"] == f"http://{_INSIDE}/snap.jpg"
        with session_scope(get_context().session_factory) as db:
            camera = db.query(Camera).filter_by(name="moved").one()
            assert camera.snapshot_uri == f"http://{_INSIDE}/snap.jpg"
            assert decrypt_credentials(camera.credentials) == {
                "username": "admin",
                "password": _SECRET,
            }

    def test_a_login_in_a_uri_with_credentials_is_422(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        resp = client.post(
            API,
            json={
                "name": "both",
                "protocol": "http",
                "snapshot_uri": f"http://admin:{_SECRET}@{_INSIDE}/snap.jpg",
                "credentials": {"username": "own", "password": "own-pw"},
                "credentials_inherit_default": False,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422
        assert _SECRET not in resp.text
        assert "remove the user:pass@" in resp.text

    def test_a_username_with_a_line_break_is_422(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        resp = client.post(
            API,
            json={
                "name": "crlf",
                "protocol": "vapix",
                "credentials": {"username": "a\r\nX-Evil: 1", "password": "x"},
                "credentials_inherit_default": False,
            },
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 422


class TestOutputRedaction:
    def test_a_legacy_row_does_not_show_its_userinfo(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        with session_scope(get_context().session_factory) as db:
            camera = Camera(
                name="legacy",
                protocol="http",
                snapshot_uri=f"http://admin:{_SECRET}@{_INSIDE}/snap.jpg",
            )
            db.add(camera)
            db.flush()
            camera_id = camera.id
        for url in (f"{API}/{camera_id}", API):
            resp = client.get(url, headers=_auth(cam_auth_token))
            assert resp.status_code == 200
            assert _SECRET not in resp.text

    def test_capture_status_redacts_last_error(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        from datetime import UTC, datetime

        from timelapse_manager.capture.supervisor import CaptureState

        with session_scope(get_context().session_factory) as db:
            camera = Camera(name="status", protocol="http")
            db.add(camera)
            db.flush()
            camera_id = camera.id
        supervisor = get_context().capture_supervisor
        assert supervisor is not None
        state = CaptureState(
            project_id=7,
            camera_id=camera_id,
            last_error=f"cannot reach http://admin:{_SECRET}@{_INSIDE}/s",
            last_error_at=datetime.now(UTC),
        )
        with patch.object(supervisor, "states_for_camera", lambda _id: [state]):
            resp = client.get(
                f"{API}/{camera_id}/capture-status", headers=_auth(cam_auth_token)
            )
        assert resp.status_code == 200
        assert _SECRET not in resp.text
        assert "***@" in resp.text


class TestProbeBinding:
    def test_detect_withholds_the_default_outside_the_subnets(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        outcome = DetectionOutcome(candidates=[], recommended_primary=None)
        with (
            patch(
                "timelapse_manager.api.cameras._resolve_camera_host_async",
                AsyncMock(return_value=None),
            ),
            patch(
                "timelapse_manager.api.cameras.detect_protocols",
                AsyncMock(return_value=outcome),
            ) as detect,
        ):
            outside = client.post(
                f"{API}/detect-protocol",
                json={"address": _OUTSIDE},
                headers=_auth(cam_auth_token),
            )
            inside = client.post(
                f"{API}/detect-protocol",
                json={"address": _INSIDE},
                headers=_auth(cam_auth_token),
            )
        assert detect.await_args_list[0].args[1] is None
        assert detect.await_args_list[1].args[1] == _DEFAULT
        assert "default login" in outside.json()["credentials_hint"]
        assert inside.json()["credentials_hint"] is None

    def test_detect_with_typed_credentials_sends_them(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        outcome = DetectionOutcome(candidates=[], recommended_primary=None)
        with (
            patch(
                "timelapse_manager.api.cameras._resolve_camera_host_async",
                AsyncMock(return_value=None),
            ),
            patch(
                "timelapse_manager.api.cameras.detect_protocols",
                AsyncMock(return_value=outcome),
            ) as detect,
        ):
            resp = client.post(
                f"{API}/detect-protocol",
                json={
                    "address": _OUTSIDE,
                    "credentials": {"username": "typed", "password": "pw"},
                },
                headers=_auth(cam_auth_token),
            )
        assert detect.await_args.args[1] == ("typed", "pw")
        assert resp.json()["credentials_hint"] is None

    def test_validate_on_an_inheriting_camera_outside_sends_nothing(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        with session_scope(get_context().session_factory) as db:
            camera = Camera(
                name="outside",
                address=_OUTSIDE,
                protocol="vapix",
                credentials_inherit_default=True,
            )
            db.add(camera)
            db.flush()
            camera_id = camera.id
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                401, headers={"WWW-Authenticate": 'Digest realm="r", nonce="n"'}
            )

        mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        supervisor = get_context().capture_supervisor
        assert supervisor is not None
        with patch.object(supervisor, "client_for", lambda purpose, pin=None: mock):
            client.post(f"{API}/{camera_id}/validate", headers=_auth(cam_auth_token))
        assert seen
        assert not [r for r in seen if r.headers.get("authorization")]

    def test_discover_enrichment_carries_no_credentials(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        found = [
            DiscoveredCamera(
                address=_INSIDE,
                protocol="onvif",
                snapshot_uri=None,
                stream_uri=None,
                geolocation=None,
                vendor=None,
            )
        ]
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, text="<nothing/>")

        mock = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        supervisor = get_context().capture_supervisor
        assert supervisor is not None
        with (
            patch.object(supervisor, "client_for", lambda purpose, pin=None: mock),
            patch(
                "timelapse_manager.api.cameras.discover_onvif",
                AsyncMock(return_value=found),
            ),
            patch(
                "timelapse_manager.cameras.discovery.resolve_camera_host_async",
                side_effect=lambda a: a,
            ),
        ):
            resp = client.post(
                f"{API}/discover", json={}, headers=_auth(cam_auth_token)
            )
        assert resp.status_code == 200, resp.text
        assert seen, "enrichment should have asked the camera"
        for request in seen:
            assert not request.headers.get("authorization")
            assert b"Password" not in request.content
            assert b"UsernameToken" not in request.content


_ = Any


class TestDetectUsesThePin:
    """Detect honours the pin of the existing camera it is run for (#233)."""

    _PEM = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"

    def _detect(
        self, client: TestClient, token: str, **extra: Any
    ) -> list[tuple[str, str | None]]:
        asked: list[tuple[str, str | None]] = []
        supervisor = get_context().capture_supervisor
        assert supervisor is not None

        def client_for(purpose: str, pin: str | None = None) -> httpx.AsyncClient:
            asked.append((purpose, pin))
            return allow_uncontained_double(httpx.AsyncClient())

        outcome = DetectionOutcome(candidates=[], recommended_primary=None)
        with (
            patch.object(supervisor, "client_for", client_for),
            patch(
                "timelapse_manager.api.cameras._resolve_camera_host_async",
                AsyncMock(return_value=None),
            ),
            patch(
                "timelapse_manager.api.cameras.detect_protocols",
                AsyncMock(return_value=outcome),
            ),
        ):
            resp = client.post(
                f"{API}/detect-protocol",
                json={"address": _INSIDE, **extra},
                headers=_auth(token),
            )
        assert resp.status_code == 200, resp.text
        return asked

    def test_a_pinned_camera_is_probed_through_its_pin(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        with session_scope(get_context().session_factory) as db:
            camera = Camera(
                name="pinned",
                address=_INSIDE,
                protocol="vapix",
                tls_pinned_cert_pem=self._PEM,
            )
            db.add(camera)
            db.flush()
            camera_id = camera.id
        assert self._detect(client, cam_auth_token, camera_id=camera_id) == [
            ("probe", self._PEM)
        ]

    def test_no_id_or_an_unknown_id_uses_the_shared_trust(
        self, client: TestClient, cam_auth_token: str
    ) -> None:
        assert self._detect(client, cam_auth_token) == [("probe", None)]
        assert self._detect(client, cam_auth_token, camera_id=424242) == [
            ("probe", None)
        ]
