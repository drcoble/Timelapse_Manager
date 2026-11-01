"""Service-layer tests for pinning a camera's certificate (#69 CAM-S1).

The pin is opt-in, set only through :func:`pin_camera_certificate` after the
operator confirms a fingerprint, and only for a camera that can actually hold
one (an ``https://`` HTTP-family camera): a pin that could never apply, because
the camera is on plain HTTP or RTSP, is refused rather than stored.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tests.unit.camera_pin_support import make_self_signed, pem_of
from timelapse_manager.cameras.tls import format_fingerprint, pem_fingerprint
from timelapse_manager.config.settings import Settings
from timelapse_manager.db.models import Camera, Event, User
from timelapse_manager.security import hash_password
from timelapse_manager.services._base import Actor, Effect
from timelapse_manager.services.cameras import (
    CameraSpec,
    create_camera,
    pin_camera_certificate,
    pin_target_url,
    pinned_fingerprint,
    unpin_camera_certificate,
    update_camera,
)
from timelapse_manager.services.errors import NotFoundError, ValidationError

_STRONG_PW = "CorrectHorse42!"


@pytest.fixture()
def session(migrated_factory) -> Iterator[Session]:  # type: ignore[no-untyped-def]
    """A migrated session; rolled back and closed after the test."""
    db = migrated_factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


@pytest.fixture()
def actor(session: Session, settings_no_autostart: Settings) -> Actor:
    """An Actor backed by a real seeded admin row (satisfies the audit FK)."""
    admin = User(
        username="root",
        auth_source="local",
        password_hash=hash_password(_STRONG_PW, settings_no_autostart.auth),
        role="admin",
        enabled=True,
    )
    session.add(admin)
    session.flush()
    return Actor.of(admin)


def _camera(
    session: Session,
    actor: Actor,
    *,
    protocol: str | None = "vapix",
    address: str | None = "https://axis-b8a44f17384a",
    snapshot_uri: str | None = None,
) -> Camera:
    spec = CameraSpec(
        name="driveway", protocol=protocol, address=address, snapshot_uri=snapshot_uri
    )
    camera = create_camera(session, actor, spec).value
    session.flush()
    return camera


def _pem(tmp_path: Path) -> str:
    return pem_of(make_self_signed(tmp_path))


class TestPinTarget:
    @pytest.mark.parametrize(
        ("protocol", "address", "snapshot", "expected"),
        [
            ("vapix", "https://axis-b8a44f17384a", None, "https://axis-b8a44f17384a"),
            ("onvif", "https://10.0.16.9:8443", None, "https://10.0.16.9:8443"),
            ("http", None, "https://10.0.16.9/snap.jpg", "https://10.0.16.9/snap.jpg"),
        ],
    )
    def test_https_cameras_can_be_pinned(
        self, protocol: str, address: str | None, snapshot: str | None, expected: str
    ) -> None:
        assert pin_target_url(protocol, address, snapshot) == expected

    @pytest.mark.parametrize(
        ("protocol", "address", "snapshot"),
        [
            ("vapix", "http://10.0.16.9", None),  # plain HTTP
            ("vapix", "10.0.16.9", None),  # scheme-less reads as http
            ("vapix", "HTTPS://10.0.16.9", None),  # parsed as http by the adapter
            ("vapix", None, None),
            ("vapix", "https://10.0.16.9", "http://10.0.16.9/snap.jpg"),
            ("onvif", "https://10.0.16.9", "HTTP://10.0.16.9/snap.jpg"),
            ("http", "https://10.0.16.9", "http://10.0.16.9/snap.jpg"),
            ("http", "https://10.0.16.9", None),  # an http camera pins its snapshot
            ("rtsp", "https://10.0.16.9", None),
            (None, "https://10.0.16.9", None),
        ],
    )
    def test_cameras_that_cannot_hold_a_pin_are_refused(
        self, protocol: str | None, address: str | None, snapshot: str | None
    ) -> None:
        with pytest.raises(ValidationError):
            pin_target_url(protocol, address, snapshot)


class TestPin:
    def test_pins_audits_and_reloads_the_session(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = _camera(session, actor)
        pem = _pem(tmp_path)
        fingerprint = pem_fingerprint(pem)

        result = pin_camera_certificate(
            session, actor, camera.id, pem, expected_fingerprint=fingerprint
        )

        assert result.effects == (Effect.CAPTURE_RECONCILE,)
        assert camera.tls_pinned_cert_pem is not None
        assert pem_fingerprint(camera.tls_pinned_cert_pem) == fingerprint
        assert pinned_fingerprint(camera) == format_fingerprint(fingerprint)
        messages = [
            e.message
            for e in session.scalars(select(Event).where(Event.scope == "camera"))
        ]
        audit = [m for m in messages if "pinned to certificate" in m]
        assert len(audit) == 1
        assert format_fingerprint(fingerprint) in audit[0]
        assert "BEGIN CERTIFICATE" not in audit[0]

    def test_the_confirmed_fingerprint_may_be_spelled_any_way(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = _camera(session, actor)
        pem = _pem(tmp_path)
        pin_camera_certificate(
            session,
            actor,
            camera.id,
            pem,
            expected_fingerprint=format_fingerprint(pem_fingerprint(pem)),
        )
        assert camera.tls_pinned_cert_pem is not None

    def test_a_different_certificate_than_confirmed_is_refused(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = _camera(session, actor)
        confirmed = _pem(tmp_path / "a")
        presented = pem_of(make_self_signed(tmp_path / "b", "b"))
        with pytest.raises(ValidationError, match="confirmed"):
            pin_camera_certificate(
                session,
                actor,
                camera.id,
                presented,
                expected_fingerprint=pem_fingerprint(confirmed),
            )
        assert camera.tls_pinned_cert_pem is None

    @pytest.mark.parametrize("bad", ["", "junk", "-----BEGIN CERTIFICATE-----\n"])
    def test_a_malformed_certificate_is_refused(
        self, session: Session, actor: Actor, bad: str
    ) -> None:
        camera = _camera(session, actor)
        with pytest.raises(ValidationError, match="not valid"):
            pin_camera_certificate(
                session, actor, camera.id, bad, expected_fingerprint="ab" * 32
            )
        assert camera.tls_pinned_cert_pem is None

    def test_an_expired_certificate_is_refused(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = _camera(session, actor)
        pem = pem_of(make_self_signed(tmp_path, expired=True))
        with pytest.raises(ValidationError, match="expired"):
            pin_camera_certificate(
                session,
                actor,
                camera.id,
                pem,
                expected_fingerprint=pem_fingerprint(pem),
            )
        assert camera.tls_pinned_cert_pem is None

    def test_a_plain_http_camera_is_refused(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = _camera(session, actor, address="http://10.0.16.9")
        pem = _pem(tmp_path)
        with pytest.raises(ValidationError, match="https"):
            pin_camera_certificate(
                session,
                actor,
                camera.id,
                pem,
                expected_fingerprint=pem_fingerprint(pem),
            )
        assert camera.tls_pinned_cert_pem is None

    def test_a_missing_camera_is_not_found(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        pem = _pem(tmp_path)
        with pytest.raises(NotFoundError):
            pin_camera_certificate(
                session, actor, 999, pem, expected_fingerprint=pem_fingerprint(pem)
            )


class TestUnpin:
    def test_unpin_clears_audits_and_reloads(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = _camera(session, actor)
        pem = _pem(tmp_path)
        pin_camera_certificate(
            session, actor, camera.id, pem, expected_fingerprint=pem_fingerprint(pem)
        )

        result = unpin_camera_certificate(session, actor, camera.id)

        assert result.effects == (Effect.CAPTURE_RECONCILE,)
        assert camera.tls_pinned_cert_pem is None
        assert pinned_fingerprint(camera) is None
        messages = [e.message for e in session.scalars(select(Event))]
        assert any("pin removed" in m for m in messages)

    def test_unpinning_an_unpinned_camera_is_a_quiet_no_op(
        self, session: Session, actor: Actor
    ) -> None:
        camera = _camera(session, actor)
        result = unpin_camera_certificate(session, actor, camera.id)
        assert result.effects == ()
        assert not any(
            "pin removed" in e.message for e in session.scalars(select(Event))
        )

    def test_a_missing_camera_is_not_found(
        self, session: Session, actor: Actor
    ) -> None:
        with pytest.raises(NotFoundError):
            unpin_camera_certificate(session, actor, 999)


class TestEditsKeepThePinMeaningful:
    def _pinned(self, session: Session, actor: Actor, tmp_path: Path) -> Camera:
        camera = _camera(session, actor)
        pem = _pem(tmp_path)
        pin_camera_certificate(
            session, actor, camera.id, pem, expected_fingerprint=pem_fingerprint(pem)
        )
        return camera

    @pytest.mark.parametrize(
        "change",
        [
            {"address": "http://axis-b8a44f17384a"},
            {"address": "axis-b8a44f17384a"},
            {"protocol": "rtsp"},
            {"snapshot_uri": "http://axis-b8a44f17384a/snap.jpg"},
        ],
    )
    def test_an_edit_cannot_downgrade_a_pinned_camera(
        self, session: Session, actor: Actor, tmp_path: Path, change: dict[str, str]
    ) -> None:
        camera = self._pinned(session, actor, tmp_path)
        spec = CameraSpec(**change, fields_set=frozenset(change))
        with pytest.raises(ValidationError):
            update_camera(session, actor, camera.id, spec)
        session.refresh(camera)
        assert camera.address == "https://axis-b8a44f17384a"
        assert camera.tls_pinned_cert_pem is not None

    def test_an_unrelated_edit_keeps_the_pin(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = self._pinned(session, actor, tmp_path)
        pem = camera.tls_pinned_cert_pem
        spec = CameraSpec(name="renamed", fields_set=frozenset({"name"}))
        update_camera(session, actor, camera.id, spec)
        assert camera.tls_pinned_cert_pem == pem

    def test_an_https_address_change_keeps_the_pin(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = self._pinned(session, actor, tmp_path)
        spec = CameraSpec(
            address="https://axis-other", fields_set=frozenset({"address"})
        )
        update_camera(session, actor, camera.id, spec)
        assert camera.tls_pinned_cert_pem is not None

    def test_after_unpinning_the_camera_may_go_back_to_http(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = self._pinned(session, actor, tmp_path)
        unpin_camera_certificate(session, actor, camera.id)
        spec = CameraSpec(address="http://10.0.16.9", fields_set=frozenset({"address"}))
        update_camera(session, actor, camera.id, spec)
        assert camera.address == "http://10.0.16.9"


class TestStreamUri:
    """A stored stream URI would let an RTSP grab carry credentials outside the pin."""

    def test_pin_target_refuses_a_stream_uri(self) -> None:
        with pytest.raises(ValidationError, match="stream URI"):
            pin_target_url(
                "onvif", "https://10.0.16.9", None, "rtsp://10.0.16.9/stream1"
            )

    def test_a_camera_with_a_stream_uri_is_not_pinned(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = _camera(session, actor)
        camera.stream_uri = "rtsp://10.0.16.9/stream1"
        pem = _pem(tmp_path)
        with pytest.raises(ValidationError, match="stream URI"):
            pin_camera_certificate(
                session,
                actor,
                camera.id,
                pem,
                expected_fingerprint=pem_fingerprint(pem),
            )
        assert camera.tls_pinned_cert_pem is None

    def test_a_pinned_camera_cannot_gain_a_stream_uri(
        self, session: Session, actor: Actor, tmp_path: Path
    ) -> None:
        camera = _camera(session, actor)
        pem = _pem(tmp_path)
        pin_camera_certificate(
            session, actor, camera.id, pem, expected_fingerprint=pem_fingerprint(pem)
        )
        spec = CameraSpec(
            stream_uri="rtsp://10.0.16.9/stream1", fields_set=frozenset({"stream_uri"})
        )
        with pytest.raises(ValidationError, match="stream URI"):
            update_camera(session, actor, camera.id, spec)
        # Clearing it is fine.
        update_camera(
            session,
            actor,
            camera.id,
            CameraSpec(stream_uri=None, fields_set=frozenset({"stream_uri"})),
        )
