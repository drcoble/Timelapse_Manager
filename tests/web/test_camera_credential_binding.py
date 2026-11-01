"""Stored and default credentials go only to the camera they belong to (#67-S3).

Owner decision OD-1 (provisional, "use the recommendation"): the global default
login is sent only to targets that resolve inside ``ssrf.allowed_private_subnets``,
never during discovery enrichment, and a camera's own stored login does not
follow an address change to a new host unless the password is retyped.

The web test settings opt no private subnet in, so each test opts ``192.168/16``
in on the running app (:func:`_allow_lan`). The outside address,
``93.184.216.34``, is public: the SSRF guard lets it through (only private space
needs an opt-in), so it is the binding rule alone that holds
the default back. Nothing here opens a socket: the probe client is a mock
transport that records every request.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from tests.web.test_camera_manage import (
    _ALLOWED_ADDRESS,
    _camera,
    _seed_camera,
)
from timelapse_manager.cameras.base import DiscoveredCamera
from timelapse_manager.cameras.clients import allow_uncontained_double
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.camera_defaults_service import (
    CameraDefaultsUpdate,
    update_settings,
)
from timelapse_manager.security.crypto import decrypt_credentials

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_OUTSIDE = "93.184.216.34"
_DEFAULT = ("fleet", "fleet-pw")


def _allow_lan() -> None:
    """Opt ``192.168.0.0/16`` in on the running app (the web settings have none)."""
    get_context().settings.ssrf.allowed_private_subnets = ["192.168.0.0/16"]


def _set_default(client: TestClient) -> None:
    client.get("/cameras")  # ensure the lifespan has run
    _allow_lan()
    with session_scope(get_context().session_factory) as db:
        update_settings(
            db,
            CameraDefaultsUpdate(
                enabled=True, username=_DEFAULT[0], password=_DEFAULT[1]
            ),
        )


class _Wire:
    """Records every request the probe client sends; answers a bare ``401``."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(
            401, headers={"WWW-Authenticate": 'Digest realm="r", nonce="n", qop="auth"'}
        )

    @property
    def credentialed(self) -> list[httpx.Request]:
        """Requests carrying an ``Authorization`` header or a WS-Security login."""
        return [
            r
            for r in self.requests
            if r.headers.get("authorization")
            or b"UsernameToken" in r.content
            or b"Password" in r.content
        ]


@contextmanager
def _probe_wire() -> Iterator[_Wire]:
    wire = _Wire()
    client = allow_uncontained_double(
        httpx.AsyncClient(transport=httpx.MockTransport(wire.handle))
    )
    supervisor = get_context().capture_supervisor
    assert supervisor is not None
    with (
        patch.object(supervisor, "client_for", lambda purpose, pin=None: client),
        # No real TCP connect for the RTSP reachability probe.
        patch(
            "timelapse_manager.cameras.probing._probe_rtsp",
            AsyncMock(side_effect=lambda address, timeout: _rtsp_miss()),
        ),
    ):
        yield wire


def _rtsp_miss() -> Any:
    from timelapse_manager.cameras.probing import Confidence, ProtocolCandidate

    return ProtocolCandidate(
        protocol="rtsp", ok=False, confidence=Confidence.LOW, detail="none"
    )


def _detect(client: TestClient, address: str, **extra: str) -> httpx.Response:
    return client.post(  # type: ignore[return-value]
        "/cameras/query",
        data={"address": address, "csrf_token": csrf_of(client, "/cameras"), **extra},
        headers=_FORM,
    )


class TestDetectAndQuery:
    def test_detect_outside_the_subnets_sends_no_credentials(
        self, admin_client: TestClient
    ) -> None:
        _set_default(admin_client)
        with _probe_wire() as wire:
            resp = _detect(admin_client, _OUTSIDE)
        assert resp.status_code == 200
        assert wire.requests, "the probe should still have been attempted"
        assert wire.credentialed == []
        assert "default login is sent only to addresses inside" in resp.text

    def test_detect_inside_the_subnets_uses_the_default(
        self, admin_client: TestClient
    ) -> None:
        _set_default(admin_client)
        with _probe_wire() as wire:
            resp = _detect(admin_client, _ALLOWED_ADDRESS)
        assert resp.status_code == 200
        assert wire.credentialed, "the default should be answered to the challenge"
        assert "default login is sent only" not in resp.text

    def test_typed_credentials_are_sent_wherever_the_operator_aims_them(
        self, admin_client: TestClient
    ) -> None:
        _set_default(admin_client)
        with _probe_wire() as wire:
            resp = _detect(admin_client, _OUTSIDE, username="typed", password="pw")
        assert wire.credentialed
        assert "default login is sent only" not in resp.text

    def test_query_outside_the_subnets_passes_no_default(
        self, admin_client: TestClient
    ) -> None:
        _set_default(admin_client)
        with patch(
            "timelapse_manager.cameras.autoquery.query_camera", AsyncMock()
        ) as q:
            q.return_value = _empty_query()
            resp = admin_client.post(
                "/cameras/query",
                data={
                    "address": _OUTSIDE,
                    "csrf_token": csrf_of(admin_client, "/cameras"),
                },
                headers=_FORM,
            )
            admin_client.post(
                "/cameras/query",
                data={
                    "address": _ALLOWED_ADDRESS,
                    "csrf_token": csrf_of(admin_client, "/cameras"),
                },
                headers=_FORM,
            )
        assert q.await_args_list[0].kwargs["default_credentials"] is None
        assert q.await_args_list[1].kwargs["default_credentials"] == _DEFAULT
        assert "default login is sent only" in resp.text


def _empty_outcome() -> Any:
    from timelapse_manager.cameras.probing import DetectionOutcome

    return DetectionOutcome(candidates=[], recommended_primary=None)


def _empty_query() -> Any:
    from timelapse_manager.cameras.autoquery import QueryResult

    return QueryResult(
        candidates=[],
        recommended_primary=None,
        ok_count=0,
        discovered_hostname=None,
        fetched_lat=None,
        fetched_lon=None,
        error_protocol=None,
        error_hostname=None,
        error_geo=None,
        geo_required_role=None,
        auth_rejected=False,
    )


class TestSavedCameras:
    def _inheriting(self, address: str, *, protocol: str = "vapix") -> int:
        camera_id = _seed_camera(
            name=f"inherit-{address}", address=address, protocol=protocol
        )
        with session_scope(get_context().session_factory) as db:
            db.get(Camera, camera_id).credentials_inherit_default = True  # type: ignore[union-attr]
        return camera_id

    def test_validate_on_an_inheriting_camera_edited_outside_sends_nothing(
        self, admin_client: TestClient
    ) -> None:
        _set_default(admin_client)
        camera_id = self._inheriting(_OUTSIDE)
        with _probe_wire() as wire:
            resp = admin_client.post(
                f"/cameras/{camera_id}/validate",
                data={"csrf_token": csrf_of(admin_client, "/cameras")},
                headers=_FORM,
            )
        assert resp.status_code == 200
        assert wire.requests
        assert wire.credentialed == []

    def test_validate_on_an_inheriting_camera_inside_still_uses_the_default(
        self, admin_client: TestClient
    ) -> None:
        _set_default(admin_client)
        camera_id = self._inheriting(_ALLOWED_ADDRESS)
        with _probe_wire() as wire:
            admin_client.post(
                f"/cameras/{camera_id}/validate",
                data={"csrf_token": csrf_of(admin_client, "/cameras")},
                headers=_FORM,
            )
        assert wire.credentialed

    def test_an_inheriting_camera_with_a_stored_uri_outside_gets_no_default(
        self, admin_client: TestClient
    ) -> None:
        _set_default(admin_client)
        camera_id = self._inheriting(_ALLOWED_ADDRESS, protocol="http")
        with session_scope(get_context().session_factory) as db:
            db.get(Camera, camera_id).snapshot_uri = f"http://{_OUTSIDE}/snap.jpg"  # type: ignore[union-attr]
        with _probe_wire() as wire:
            admin_client.post(
                f"/cameras/{camera_id}/validate",
                data={"csrf_token": csrf_of(admin_client, "/cameras")},
                headers=_FORM,
            )
        assert wire.requests
        assert wire.credentialed == []


class TestDiscoveryCarriesNoCredentials:
    def test_enrichment_sends_no_login_even_with_a_default_configured(
        self, operator_client: TestClient
    ) -> None:
        _set_default(operator_client)
        found = [
            DiscoveredCamera(
                address=_ALLOWED_ADDRESS,
                protocol="onvif",
                snapshot_uri=None,
                stream_uri=None,
                geolocation=None,
                vendor=None,
            )
        ]
        with (
            _probe_wire() as wire,
            patch(
                "timelapse_manager.cameras.discover_onvif",
                new=AsyncMock(return_value=found),
            ),
            patch(
                "timelapse_manager.cameras.discovery.resolve_camera_host_async",
                side_effect=lambda a: a,
            ),
        ):
            resp = operator_client.post(
                "/cameras/discover",
                data={"csrf_token": csrf_of(operator_client, "/cameras")},
                headers=_FORM,
            )
        assert resp.status_code == 200
        assert wire.requests, "enrichment should have asked the camera"
        assert wire.credentialed == []


class TestEditClearsStoredCredentialsOnAHostChange:
    def _edit(self, client: TestClient, camera_id: int, **form: str) -> httpx.Response:
        data = {
            "name": f"cam-{camera_id}",
            "protocol": "vapix",
            "csrf_token": csrf_of(client, "/cameras"),
            **form,
        }
        return client.post(f"/cameras/{camera_id}/edit", data=data, headers=_FORM)  # type: ignore[return-value]

    def _seed(self) -> int:
        _allow_lan()
        return _seed_camera(
            name="bound",
            credentials={"username": "cam-user", "password": "cam-pass"},
        )

    def test_a_new_address_with_a_blank_password_drops_the_login(
        self, admin_client: TestClient
    ) -> None:
        camera_id = self._seed()
        resp = self._edit(
            admin_client, camera_id, address="192.168.1.99", username="cam-user"
        )
        assert resp.status_code == 200
        camera = _camera(camera_id)
        assert camera is not None
        assert camera.address == "192.168.1.99"
        assert camera.credentials is None
        assert "re-enter the password" in resp.text
        assert "cam-pass" not in resp.text

    def test_the_same_address_keeps_the_login(self, admin_client: TestClient) -> None:
        camera_id = self._seed()
        resp = self._edit(
            admin_client, camera_id, address=_ALLOWED_ADDRESS, username="cam-user"
        )
        assert resp.status_code == 200
        camera = _camera(camera_id)
        assert camera is not None and camera.credentials is not None
        assert decrypt_credentials(camera.credentials)["password"] == "cam-pass"  # type: ignore[index]
        assert "re-enter the password" not in resp.text

    def test_a_retyped_password_goes_with_the_new_address(
        self, admin_client: TestClient
    ) -> None:
        camera_id = self._seed()
        self._edit(
            admin_client,
            camera_id,
            address="192.168.1.99",
            username="cam-user",
            password="new-pass",
        )
        camera = _camera(camera_id)
        assert camera is not None and camera.credentials is not None
        assert decrypt_credentials(camera.credentials)["password"] == "new-pass"  # type: ignore[index]

    def test_a_scheme_or_port_change_on_the_same_host_keeps_the_login(
        self, admin_client: TestClient
    ) -> None:
        camera_id = self._seed()
        self._edit(
            admin_client,
            camera_id,
            address=f"https://{_ALLOWED_ADDRESS}:8443",
            username="cam-user",
        )
        camera = _camera(camera_id)
        assert camera is not None and camera.credentials is not None

    def test_a_new_snapshot_uri_host_drops_the_login_too(
        self, admin_client: TestClient
    ) -> None:
        camera_id = self._seed()
        self._edit(
            admin_client,
            camera_id,
            address=_ALLOWED_ADDRESS,
            username="cam-user",
            snapshot_uri=f"http://{_OUTSIDE}/snap.jpg",
        )
        camera = _camera(camera_id)
        assert camera is not None
        assert camera.credentials is None
        assert camera.snapshot_uri == f"http://{_OUTSIDE}/snap.jpg"

    def test_inheriting_the_default_is_unaffected_by_the_rule(
        self, admin_client: TestClient
    ) -> None:
        camera_id = self._seed()
        self._edit(
            admin_client,
            camera_id,
            address="192.168.1.99",
            credentials_inherit_default="on",
        )
        camera = _camera(camera_id)
        assert camera is not None
        assert camera.credentials is None
        assert camera.credentials_inherit_default is True


class TestQueryBasicOptIn:
    """#332: the form's Basic-over-HTTP opt-in reaches the Query probe."""

    def _query(self, client: TestClient, **extra: str) -> Any:
        client.get("/cameras")  # ensure the lifespan (capture supervisor) has run
        _allow_lan()
        with patch(
            "timelapse_manager.cameras.autoquery.query_camera", AsyncMock()
        ) as q:
            q.return_value = _empty_query()
            client.post(
                "/cameras/query",
                data={
                    "address": _ALLOWED_ADDRESS,
                    "csrf_token": csrf_of(client, "/cameras"),
                    **extra,
                },
                headers=_FORM,
            )
        return q.await_args.kwargs

    def test_ticked_opt_in_is_passed_on(self, admin_client: TestClient) -> None:
        kwargs = self._query(admin_client, allow_basic_over_http="on")
        assert kwargs["allow_basic_over_http"] is True

    def test_unticked_opt_in_is_not(self, admin_client: TestClient) -> None:
        assert self._query(admin_client)["allow_basic_over_http"] is False
