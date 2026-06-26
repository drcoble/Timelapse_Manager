"""Integration: the default-credential fallback at adapter-build time.

Exercises the real composition the validate and capture call sites use --
``resolve_default_credentials`` (decrypt-at-use over a persisted, encrypted row)
folded into ``effective_credentials`` against persisted camera rows -- and that
``build_adapter`` carries the resolved credentials onto the constructed adapter.

The fallback is opt-in per camera: a credential-free camera uses the default only
when its inherit flag is on; per-camera credentials always override the default;
and a deliberately-open camera (flag off, no creds) stays open.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from timelapse_manager.cameras.registry import build_adapter, effective_credentials
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.camera_defaults_service import (
    CameraDefaultsUpdate,
    resolve_default_credentials,
    update_settings,
)
from timelapse_manager.security.crypto import encrypt_credentials

_DEFAULT = ("default-user", "default-secret")


def _enable_default(factory) -> None:
    with session_scope(factory) as session:
        update_settings(
            session,
            CameraDefaultsUpdate(
                enabled=True,
                username=_DEFAULT[0],
                password=_DEFAULT[1],
            ),
        )


def _seed_camera(
    factory,
    *,
    name: str,
    inherit: bool,
    credentials: dict[str, object] | None = None,
) -> int:
    with session_scope(factory) as session:
        cam = Camera(
            name=name,
            address="192.168.1.50",
            protocol="http",
            snapshot_uri="http://192.168.1.50/snap.jpg",
            credentials=encrypt_credentials(credentials),
            credentials_inherit_default=inherit,
        )
        session.add(cam)
        session.flush()
        return cam.id


def _resolve_for(factory, camera_id: int) -> tuple[str, str] | None:
    """Mirror the call sites: resolve default + camera in one session, decide."""
    with session_scope(factory) as session:
        cam = session.get(Camera, camera_id)
        assert cam is not None
        default = resolve_default_credentials(session)
        return effective_credentials(cam, default)


class TestFallbackResolution:
    def test_credential_free_inheriting_camera_uses_default(
        self, migrated_factory
    ) -> None:
        _enable_default(migrated_factory)
        cam_id = _seed_camera(migrated_factory, name="inherit-cam", inherit=True)
        assert _resolve_for(migrated_factory, cam_id) == _DEFAULT

    def test_credential_free_non_inheriting_camera_stays_open(
        self, migrated_factory
    ) -> None:
        _enable_default(migrated_factory)
        cam_id = _seed_camera(migrated_factory, name="open-cam", inherit=False)
        assert _resolve_for(migrated_factory, cam_id) is None

    def test_own_credentials_override_default(self, migrated_factory) -> None:
        _enable_default(migrated_factory)
        cam_id = _seed_camera(
            migrated_factory,
            name="own-cam",
            inherit=True,
            credentials={"username": "own-user", "password": "own-secret"},
        )
        assert _resolve_for(migrated_factory, cam_id) == ("own-user", "own-secret")

    def test_inheriting_camera_open_when_default_disabled(
        self, migrated_factory
    ) -> None:
        # Default exists but is disabled: an inheriting camera still stays open.
        with session_scope(migrated_factory) as session:
            update_settings(
                session,
                CameraDefaultsUpdate(
                    enabled=False,
                    username=_DEFAULT[0],
                    password=_DEFAULT[1],
                ),
            )
        cam_id = _seed_camera(migrated_factory, name="disabled-default", inherit=True)
        assert _resolve_for(migrated_factory, cam_id) is None


class TestBuildAdapterCarriesDefault:
    """``build_adapter`` does no I/O, so a mock client is sufficient; the focus is
    which effective credentials end up on the constructed adapter."""

    def _adapter_credentials(self, factory, camera_id: int) -> tuple[str, str] | None:
        with session_scope(factory) as session:
            cam = session.get(Camera, camera_id)
            assert cam is not None
            default = resolve_default_credentials(session)
            session.expunge(cam)
        adapter = build_adapter(cam, MagicMock(), default_credentials=default)
        # The HTTP/JPEG adapter exposes the credentials it will use.
        return adapter._credentials  # type: ignore[attr-defined,no-any-return]

    def test_adapter_built_with_default_credentials(self, migrated_factory) -> None:
        _enable_default(migrated_factory)
        cam_id = _seed_camera(migrated_factory, name="adapter-cam", inherit=True)
        assert self._adapter_credentials(migrated_factory, cam_id) == _DEFAULT

    def test_adapter_open_when_flag_off(self, migrated_factory) -> None:
        _enable_default(migrated_factory)
        cam_id = _seed_camera(migrated_factory, name="adapter-open", inherit=False)
        assert self._adapter_credentials(migrated_factory, cam_id) is None
