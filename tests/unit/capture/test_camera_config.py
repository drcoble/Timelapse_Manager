"""The merged capture-config loader (#66 C12, #69 CAM-R8).

``load_capture_config`` reads the camera and, only when the camera needs them,
the global default credentials, in one session. These tests pin:

* one session per load;
* the default is read only for an inheriting camera without its own
  credentials, and the effective credentials are identical to resolving the
  default unconditionally (today's ``effective_credentials`` behaviour) for
  every combination;
* a missing camera gives ``None``, and the capture path still raises the same
  ``RuntimeError`` as before;
* secrets stay out of the config's ``repr``.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tests.unit.credential_binding_support import (  # noqa: F401
    _allowed_private_subnets,
)
from tests.unit.test_campaign_bounds import (
    _INTERVAL,
    _make_settings,
    _permissive_disk_monitor,
)
from timelapse_manager.cameras.registry import effective_credentials
from timelapse_manager.capture.camera_config import (
    CaptureCameraConfig,
    has_own_credentials,
    load_capture_config,
    needs_default_credentials,
)
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
)
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.camera_defaults_service import (
    CameraDefaultsUpdate,
    resolve_default_credentials,
    update_settings,
)
from timelapse_manager.security.crypto import encrypt_credentials

_RESOLVE = "timelapse_manager.capture.camera_config.resolve_default_credentials"
_DEFAULT = ("fallback", "fallback-pw")


def _add_camera(
    factory: sessionmaker[Session], *, own: bool, inherit: bool, name: str
) -> int:
    credentials = (
        encrypt_credentials({"username": "own", "password": "own-pw"}) if own else None
    )
    with session_scope(factory) as session:
        camera = Camera(
            name=name,
            address="10.0.0.5",
            protocol="vapix",
            credentials=credentials,
            credentials_inherit_default=inherit,
        )
        session.add(camera)
        session.flush()
        return camera.id


def _set_default(factory: sessionmaker[Session], *, present: bool) -> None:
    with session_scope(factory) as session:
        update_settings(
            session,
            CameraDefaultsUpdate(
                enabled=present, username=_DEFAULT[0], password=_DEFAULT[1]
            ),
        )


@pytest.mark.parametrize(
    ("own", "inherit", "default_present"),
    list(itertools.product([True, False], repeat=3)),
)
def test_effective_credentials_match_unconditional_resolution(
    migrated_factory: sessionmaker[Session],
    own: bool,
    inherit: bool,
    default_present: bool,
) -> None:
    """CAM-R8 changes when the default is read, never which credentials win."""
    _set_default(migrated_factory, present=default_present)
    camera_id = _add_camera(migrated_factory, own=own, inherit=inherit, name="cam")

    with patch(_RESOLVE, wraps=resolve_default_credentials) as resolve:
        config = load_capture_config(migrated_factory, camera_id)
    assert config is not None

    # Today's behaviour: the default is always resolved, then discarded by
    # effective_credentials when the camera does not use it.
    with session_scope(migrated_factory) as session:
        always_resolved = resolve_default_credentials(session)
    expected = effective_credentials(config, always_resolved)

    assert effective_credentials(config, config.default_credentials) == expected
    needed = inherit and not own
    assert resolve.call_count == (1 if needed else 0)
    if not needed:
        assert config.default_credentials is None
    if own:
        assert expected == ("own", "own-pw")
    elif inherit and default_present:
        assert expected == _DEFAULT
    else:
        assert expected is None


def test_loads_in_one_session(migrated_factory: sessionmaker[Session]) -> None:
    _set_default(migrated_factory, present=True)
    camera_id = _add_camera(migrated_factory, own=False, inherit=True, name="one")
    opened = 0

    def counting_factory() -> Session:
        nonlocal opened
        opened += 1
        return migrated_factory()

    config = load_capture_config(counting_factory, camera_id)  # type: ignore[arg-type]

    assert opened == 1
    assert config is not None
    assert config.default_credentials == _DEFAULT


def test_carries_plain_camera_fields(migrated_factory: sessionmaker[Session]) -> None:
    camera_id = _add_camera(migrated_factory, own=True, inherit=False, name="plain")
    config = load_capture_config(migrated_factory, camera_id)

    assert isinstance(config, CaptureCameraConfig)
    assert config.protocol == "vapix"
    assert config.address == "10.0.0.5"
    assert config.credentials_inherit_default is False
    assert config.updated_at is not None


def test_missing_camera_returns_none(migrated_factory: sessionmaker[Session]) -> None:
    assert load_capture_config(migrated_factory, 999_999) is None


def test_repr_hides_credentials() -> None:
    config = CaptureCameraConfig(
        protocol="vapix",
        address="10.0.0.5",
        credentials={"username": "own", "password": "secret-own"},
        credentials_inherit_default=True,
        snapshot_uri=None,
        stream_uri=None,
        default_resolution=None,
        default_credentials=("fallback", "secret-default"),
    )
    text = repr(config)
    assert "secret-own" not in text
    assert "secret-default" not in text
    assert "10.0.0.5" in text


@pytest.mark.parametrize(
    ("credentials", "expected"),
    [
        (None, False),
        ({}, False),
        ({"username": ""}, False),
        ({"password": "only"}, False),
        ({"username": "u"}, True),
        ("not-a-dict", False),
    ],
)
def test_has_own_credentials(credentials: Any, expected: bool) -> None:
    assert has_own_credentials(credentials) is expected


def test_needs_default_credentials_requires_inherit() -> None:
    class _Cam:
        credentials = None
        credentials_inherit_default = False

    assert needs_default_credentials(_Cam()) is False
    _Cam.credentials_inherit_default = True
    assert needs_default_credentials(_Cam()) is True


async def test_capture_once_raises_for_a_deleted_camera(
    migrated_factory: sessionmaker[Session], tmp_path: Path
) -> None:
    supervisor = CaptureSupervisor(
        _make_settings(tmp_path),
        migrated_factory,
        disk_monitor=_permissive_disk_monitor(),
    )
    target = CaptureTarget(
        project_id=1, project_name="gone", camera_id=999_999, interval_seconds=_INTERVAL
    )
    state = CaptureState(project_id=1, camera_id=999_999)

    with pytest.raises(RuntimeError, match="camera 999999 no longer exists"):
        await supervisor._capture_once(target, state)
    await supervisor.stop()
