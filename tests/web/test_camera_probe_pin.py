"""Query (web) and Detect (API) honour the pin of the camera they are run for (#233).

A pinned camera trusts its certificate alone, so a probe of it through the shared
trust fails TLS against its self-signed certificate. The edit form posts the
camera's id (``probe_camera_id``; ``camera_id`` in the API body) and the probe
uses that camera's pinned client. An ad-hoc probe of a new address, or an
unknown id, still uses the shared trust.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from tests.web.test_camera_manage import _ALLOWED_ADDRESS, _seed_camera
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_PIN = "-----BEGIN CERTIFICATE-----\nPINNED\n-----END CERTIFICATE-----\n"


def _pin(camera_id: int, pem: str | None = _PIN) -> None:
    with session_scope(get_context().session_factory) as db:
        camera = db.get(Camera, camera_id)
        assert camera is not None
        camera.tls_pinned_cert_pem = pem


@contextmanager
def _recording_probe_client() -> Iterator[list[tuple[str, str | None]]]:
    """Record the ``(purpose, pin)`` of every client the routes ask for."""
    asked: list[tuple[str, str | None]] = []
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(404))
    )
    supervisor = get_context().capture_supervisor
    assert supervisor is not None

    def client_for(purpose: str, pin: str | None = None) -> httpx.AsyncClient:
        asked.append((purpose, pin))
        return client

    with (
        patch.object(supervisor, "client_for", client_for),
        patch(
            "timelapse_manager.cameras.probing._probe_rtsp",
            AsyncMock(side_effect=lambda address, timeout: _rtsp_miss()),
        ),
    ):
        yield asked


def _rtsp_miss():  # type: ignore[no-untyped-def]
    from timelapse_manager.cameras.probing import Confidence, ProtocolCandidate

    return ProtocolCandidate(
        protocol="rtsp", ok=False, confidence=Confidence.LOW, detail="none"
    )


def _post(client: TestClient, path: str, **fields: str) -> httpx.Response:
    client.get("/cameras")  # ensure the lifespan has run
    get_context().settings.ssrf.allowed_private_subnets = ["192.168.0.0/16"]
    return client.post(  # type: ignore[return-value]
        path,
        data={
            "address": _ALLOWED_ADDRESS,
            "csrf_token": csrf_of(client, "/cameras"),
            **fields,
        },
        headers=_FORM,
    )


class TestQueryAndDetectUseThePin:
    def test_query_for_a_pinned_camera_uses_the_pinned_client(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="pinned-query")
        _pin(camera_id)
        with _recording_probe_client() as asked:
            resp = _post(admin_client, "/cameras/query", probe_camera_id=str(camera_id))
        assert resp.status_code == 200
        assert ("probe", _PIN) in asked
        assert ("probe", None) not in asked

    def test_an_unpinned_camera_uses_the_shared_trust(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="unpinned")
        with _recording_probe_client() as asked:
            _post(admin_client, "/cameras/query", probe_camera_id=str(camera_id))
        assert asked and all(pin is None for _purpose, pin in asked)

    def test_an_adhoc_probe_and_a_bad_id_use_the_shared_trust(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="other-pinned")
        _pin(camera_id)
        with _recording_probe_client() as asked:
            _post(admin_client, "/cameras/query")  # a new camera: no id
            _post(admin_client, "/cameras/query", probe_camera_id="not-a-number")
            _post(admin_client, "/cameras/query", probe_camera_id="999999")
        assert asked and all(pin is None for _purpose, pin in asked)

    def test_the_edit_form_posts_its_camera_id_and_the_add_form_does_not(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="form-id")
        edit = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        assert f'name="probe_camera_id" value="{camera_id}"' in edit
        assert "probe_camera_id" not in admin_client.get("/cameras/add-form").text
