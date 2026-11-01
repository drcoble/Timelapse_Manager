"""The capture loader binds the default login to the camera's hosts (#67-S3, OD-1).

A scheduled capture, a one-shot and an event listener all load the camera through
:func:`load_capture_config`. An inheriting camera whose address (or stored
snapshot/stream URI) resolves outside ``allowed_private_subnets`` is loaded
**without** the default, so editing a camera onto a public host never sends the
fleet login there -- while a camera in the allowed subnet, in either address
form, keeps working. The reconcile fingerprint is unaffected (it is computed
from the stored rows, which is what each reconcile pass compares it with).
"""

from __future__ import annotations

import socket

import pytest
from sqlalchemy.orm import Session, sessionmaker

from timelapse_manager.capture.camera_config import (
    config_version,
    load_capture_config,
    load_defaults_row,
    resolve_capture_credentials,
)
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.security import credential_binding, ssrf
from timelapse_manager.security.camera_defaults_service import (
    CameraDefaultsUpdate,
    update_settings,
)
from timelapse_manager.security.crypto import encrypt_credentials

_DEFAULT = ("fleet", "fleet-pw")


@pytest.fixture(autouse=True)
def _lan_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        credential_binding, "_configured_subnets", lambda: ["10.0.16.0/24"]
    )


@pytest.fixture()
def factory(migrated_factory: sessionmaker[Session]) -> sessionmaker[Session]:
    with session_scope(migrated_factory) as session:
        update_settings(
            session,
            CameraDefaultsUpdate(
                enabled=True, username=_DEFAULT[0], password=_DEFAULT[1]
            ),
        )
    return migrated_factory


def _add(
    factory: sessionmaker[Session],
    address: str | None,
    *,
    protocol: str = "vapix",
    own: bool = False,
    **fields: str | None,
) -> int:
    with session_scope(factory) as session:
        camera = Camera(
            name=f"cam-{address}-{protocol}-{own}",
            address=address,
            protocol=protocol,
            credentials=(
                encrypt_credentials({"username": "own", "password": "own-pw"})
                if own
                else None
            ),
            credentials_inherit_default=True,
            **fields,
        )
        session.add(camera)
        session.flush()
        return camera.id


@pytest.mark.parametrize(
    "address",
    ["10.0.16.61", "https://10.0.16.61", "https://10.0.16.61:8443/x", "10.0.16.61:80"],
)
def test_an_inheriting_camera_inside_the_subnet_gets_the_default(
    factory: sessionmaker[Session], address: str
) -> None:
    config = load_capture_config(factory, _add(factory, address))
    assert config is not None
    assert config.default_credentials == _DEFAULT
    assert config.default_withheld is False
    assert resolve_capture_credentials(config) == _DEFAULT


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "https://93.184.216.34", "10.0.17.9"]
)
def test_an_inheriting_camera_outside_the_subnet_gets_none(
    factory: sessionmaker[Session], address: str
) -> None:
    config = load_capture_config(factory, _add(factory, address))
    assert config is not None
    assert config.default_credentials is None
    assert config.default_withheld is True  # retried on use (#269)
    assert resolve_capture_credentials(config) is None


def test_a_stored_uri_outside_withholds_the_default(
    factory: sessionmaker[Session],
) -> None:
    camera_id = _add(
        factory,
        "10.0.16.61",
        protocol="http",
        snapshot_uri="http://93.184.216.34/s.jpg",
    )
    config = load_capture_config(factory, camera_id)
    assert config is not None and config.default_credentials is None


def test_an_http_camera_is_bound_by_its_snapshot_uri(
    factory: sessionmaker[Session],
) -> None:
    inside = _add(factory, None, protocol="http", snapshot_uri="http://10.0.16.7/s.jpg")
    config = load_capture_config(factory, inside)
    assert config is not None and config.default_credentials == _DEFAULT


def test_a_hostname_is_resolved_and_must_land_inside(
    factory: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    table = {"cam.lan": ["10.0.16.61"], "mixed.lan": ["10.0.16.61", "93.184.216.34"]}

    def getaddrinfo(host: str, *_a: object, **_k: object) -> list[tuple]:
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "nope")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in table[host]
        ]

    ssrf.clear_resolution_cache()
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    ok = load_capture_config(factory, _add(factory, "https://cam.lan"))
    mixed = load_capture_config(factory, _add(factory, "https://mixed.lan"))
    gone = load_capture_config(factory, _add(factory, "https://gone.lan"))
    assert ok is not None and ok.default_credentials == _DEFAULT
    assert mixed is not None and mixed.default_credentials is None
    assert gone is not None and gone.default_credentials is None
    assert gone.default_withheld is True  # a DNS miss is not a permanent verdict


def test_a_camera_with_its_own_login_is_not_bound(
    factory: sessionmaker[Session],
) -> None:
    config = load_capture_config(factory, _add(factory, "93.184.216.34", own=True))
    assert config is not None
    assert resolve_capture_credentials(config) == ("own", "own-pw")


def test_the_fingerprint_is_that_of_the_stored_rows(
    factory: sessionmaker[Session],
) -> None:
    camera_id = _add(factory, "93.184.216.34")
    config = load_capture_config(factory, camera_id)
    assert config is not None
    with session_scope(factory) as session:
        camera = session.get(Camera, camera_id)
        assert camera is not None
        stored = config_version(camera, load_defaults_row(session))
    assert config.config_version == stored
