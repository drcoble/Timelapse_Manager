"""Integration tests: camera credential encryption at rest.

A camera's stored credential document has its secret fields (password) encrypted
at rest; the username stays readable for display. The single read chokepoint
(``credentials_from``, used by every adapter build) decrypts transparently, so
the capture path is unaware of the encryption and sees the original plaintext.
Legacy plaintext credential documents pass through unchanged.
"""

from __future__ import annotations

from timelapse_manager.cameras.http_jpeg import credentials_from
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.security.crypto import encrypt_credentials, is_encrypted


def _make_camera(factory, credentials: dict) -> int:
    """Persist a camera the way the create routes do (encrypt on write)."""
    with session_scope(factory) as session:
        camera = Camera(
            name="cam",
            protocol="http",
            address="cam.example",
            snapshot_uri="http://cam.example/snap.jpg",
            credentials=encrypt_credentials(credentials),
        )
        session.add(camera)
        session.flush()
        return camera.id


class TestCameraCredentialsEncryptedAtRest:
    def test_password_column_holds_ciphertext_not_plaintext(
        self, migrated_factory
    ) -> None:
        cam_id = _make_camera(
            migrated_factory, {"username": "admin", "password": "s3cr3t-pw"}
        )
        with session_scope(migrated_factory) as session:
            row = session.get(Camera, cam_id)
            assert row is not None
            stored = row.credentials or {}
        assert stored.get("password") != "s3cr3t-pw", (
            "camera password stored as plaintext — encryption not applied"
        )
        assert is_encrypted(str(stored.get("password")))

    def test_username_stays_readable(self, migrated_factory) -> None:
        cam_id = _make_camera(
            migrated_factory, {"username": "admin", "password": "s3cr3t-pw"}
        )
        with session_scope(migrated_factory) as session:
            row = session.get(Camera, cam_id)
            assert row is not None
            assert (row.credentials or {}).get("username") == "admin"

    def test_read_chokepoint_round_trips_to_plaintext(self, migrated_factory) -> None:
        """credentials_from must hand the adapter the original plaintext."""
        cam_id = _make_camera(
            migrated_factory, {"username": "admin", "password": "s3cr3t-pw"}
        )
        with session_scope(migrated_factory) as session:
            row = session.get(Camera, cam_id)
            assert credentials_from(row) == ("admin", "s3cr3t-pw")

    def test_legacy_plaintext_credentials_still_readable(
        self, migrated_factory
    ) -> None:
        """A pre-encryption (plaintext) credential row decrypts to itself."""
        with session_scope(migrated_factory) as session:
            camera = Camera(
                name="legacy",
                protocol="http",
                address="cam.example",
                snapshot_uri="http://cam.example/snap.jpg",
                credentials={"username": "admin", "password": "legacy-plain"},
            )
            session.add(camera)
            session.flush()
            cam_id = camera.id
        with session_scope(migrated_factory) as session:
            row = session.get(Camera, cam_id)
            assert credentials_from(row) == ("admin", "legacy-plain")
