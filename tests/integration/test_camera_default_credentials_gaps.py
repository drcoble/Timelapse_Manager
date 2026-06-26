"""Coverage gap tests for the default-camera-credentials feature.

Closes four specific coverage holes not addressed by the existing test files:

1. Inv 1 — Per-camera password leak: the edit form for a camera with own
   credentials (inherit OFF) must not render the plaintext or ciphertext
   password in any form field value.

2. Inv 2 — Enabled-toggle round-trip: toggling enabled OFF and back ON while
   submitting the masked sentinel must not corrupt the stored secret.

3. Inv 4 — Server-side smuggle guard: a POST with credentials_inherit_default
   ON *and* own username/password in the same request (as a tampered or
   scripted submission) must NOT persist the own creds — the server enforces
   the invariant independently of client-side disabled-input behaviour.  Tested
   for both the create path and the edit path.

4. Inv 5 — Migration data-preservation + singleton constraint:
   - existing camera rows receive credentials_inherit_default=0 after upgrade;
   - inserting a CameraDefaultCredentials row with id != 1 raises IntegrityError.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from tests.conftest import csrf_of
from timelapse_manager.cameras.http_jpeg import credentials_from
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Camera, CameraDefaultCredentials
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.camera_defaults_service import (
    MASK_SENTINEL,
    CameraDefaultsUpdate,
    resolve_default_credentials,
    update_settings,
)
from timelapse_manager.security.crypto import encrypt_credentials

# An address inside the test settings' allowed private subnets (192.168.0.0/16).
_ALLOWED_ADDRESS = "192.168.1.50"


# ---------------------------------------------------------------------------
# Inv 1: per-camera edit form must not reveal the stored password
# ---------------------------------------------------------------------------


class TestPerCameraPasswordNotEchoed:
    """The edit-form must never render the camera's own password (Inv 1)."""

    def test_edit_form_does_not_reveal_own_password(
        self, admin_client: TestClient
    ) -> None:
        """GET /cameras/{id}/edit-form for a camera with own creds must not echo
        the plaintext or encrypted password in any field value.

        The camera is seeded with inherit=False and an own username+password so
        there is actually a secret to leak.  Previous tests only seeded cameras
        with inherit=True and no credentials, making the no-leak assertion vacuous.
        """
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = Camera(
                name="own-creds-cam",
                address=_ALLOWED_ADDRESS,
                protocol="vapix",
                credentials=encrypt_credentials(
                    {"username": "own-user", "password": "s3cr3t-camera-pass"}
                ),
                credentials_inherit_default=False,
            )
            db.add(cam)
            db.flush()
            camera_id = cam.id

        resp = admin_client.get(f"/cameras/{camera_id}/edit-form")
        assert resp.status_code == 200

        # The plaintext password must never appear.
        assert "s3cr3t-camera-pass" not in resp.text

        # The encrypted ciphertext must not leak into the rendered HTML either.
        # Retrieve the ciphertext from the DB and confirm it is absent.
        with session_scope(ctx.session_factory) as db:
            stored_cam = db.get(Camera, camera_id)
            assert stored_cam is not None
            raw_creds = stored_cam.credentials or {}
        stored_ciphertext = raw_creds.get("password", "")
        if stored_ciphertext:
            assert stored_ciphertext not in resp.text

        # The password input exists (so its presence is tested) but carries no value.
        assert 'name="password"' in resp.text
        # The value attribute of the password input must be absent or empty.
        # The template must never set value="<anything>" on the password field.
        assert 'value="s3cr3t-camera-pass"' not in resp.text
        assert f'value="{stored_ciphertext}"' not in resp.text


# ---------------------------------------------------------------------------
# Inv 2: enabled-toggle round-trip must not corrupt the stored secret
# ---------------------------------------------------------------------------


class TestEnabledTogglePreservesSecret:
    """Toggling enabled off then on while sending the mask must keep the
    stored ciphertext untouched (Inv 2 — the sub-clause not previously tested).
    """

    def test_toggle_disabled_then_enabled_keeps_secret(
        self, migrated_factory: object
    ) -> None:
        original_password = "toggle-round-trip-secret"

        # Write the initial row: enabled=True with a real password.
        with session_scope(migrated_factory) as session:  # type: ignore[arg-type]
            update_settings(
                session,
                CameraDefaultsUpdate(
                    enabled=True,
                    username="fallback-user",
                    password=original_password,
                ),
            )

        # Disable (toggled off) while submitting the mask — password unchanged.
        with session_scope(migrated_factory) as session:  # type: ignore[arg-type]
            update_settings(
                session,
                CameraDefaultsUpdate(
                    enabled=False,
                    username="fallback-user",
                    password=MASK_SENTINEL,
                ),
            )

        # Re-enable while again submitting the mask.
        with session_scope(migrated_factory) as session:  # type: ignore[arg-type]
            update_settings(
                session,
                CameraDefaultsUpdate(
                    enabled=True,
                    username="fallback-user",
                    password=MASK_SENTINEL,
                ),
            )

        # The decrypted secret must be the original value after both toggles.
        with session_scope(migrated_factory) as session:  # type: ignore[arg-type]
            result = resolve_default_credentials(session)

        assert result == ("fallback-user", original_password)


# ---------------------------------------------------------------------------
# Inv 4: server-side smuggle guard — own creds rejected when inherit=ON
# ---------------------------------------------------------------------------


class TestServerSideSmuggleGuard:
    """POST with inherit=ON AND own credentials must not persist own creds (Inv 4).

    This tests the guard in the server handler, independent of the client-side
    disabled-input behaviour.  A tampered or scripted request that includes
    username+password alongside credentials_inherit_default=on must NOT write
    the submitted creds to the camera row.
    """

    def test_create_with_inherit_on_and_own_creds_does_not_persist_creds(
        self, admin_client: TestClient
    ) -> None:
        """Smuggling own credentials at camera-create time must be blocked.

        The create handler guards at line 1274: ``if not inherit_default and
        (username or password)``.  When inherit_default is True the condition
        is False, so credentials is None regardless of what was submitted.
        """
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            "/cameras",
            data={
                "name": "smuggle-create-cam",
                "protocol": "vapix",
                # No address — avoids SSRF validation, not the focus of this test.
                "credentials_inherit_default": "on",
                # Tampered: own username + password alongside the inherit flag.
                "username": "injected-user",
                "password": "injected-pass",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # Should succeed (303 redirect to /cameras).
        assert resp.status_code == 303

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = db.query(Camera).filter_by(name="smuggle-create-cam").one()
            assert cam.credentials_inherit_default is True
            # The server must have discarded the submitted own credentials.
            assert credentials_from(cam) is None

    def test_edit_with_inherit_on_and_own_creds_does_not_persist_creds(
        self, admin_client: TestClient
    ) -> None:
        """Smuggling own credentials at camera-edit time must be blocked.

        The edit handler guards at line 1473: ``if inherit_default: credentials =
        None`` — own creds are unconditionally cleared when inherit is on.
        """
        ctx = get_context()
        # Seed a camera that previously had own credentials.
        with session_scope(ctx.session_factory) as db:
            cam = Camera(
                name="smuggle-edit-cam",
                address=_ALLOWED_ADDRESS,
                protocol="vapix",
                credentials=encrypt_credentials(
                    {"username": "existing-user", "password": "existing-pass"}
                ),
                credentials_inherit_default=False,
            )
            db.add(cam)
            db.flush()
            camera_id = cam.id

        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "smuggle-edit-cam",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "credentials_inherit_default": "on",
                # Tampered: still sending own creds even though inherit is on.
                "username": "injected-user",
                "password": "injected-pass",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # The edit handler returns 200 with the refreshed camera-row fragment.
        assert resp.status_code == 200

        with session_scope(ctx.session_factory) as db:
            updated = db.get(Camera, camera_id)
            assert updated is not None
            assert updated.credentials_inherit_default is True
            # No own credentials must remain — the server cleared them.
            assert credentials_from(updated) is None


# ---------------------------------------------------------------------------
# Inv 5: migration data-preservation + singleton CheckConstraint
# ---------------------------------------------------------------------------


class TestMigration009DataPreservation:
    """Upgrading to 009 sets credentials_inherit_default=0 on existing rows (Inv 5)."""

    def test_existing_camera_rows_get_inherit_false_after_upgrade(
        self, alembic_cfg: Config, tmp_db_url: str
    ) -> None:
        """Camera rows written before migration 009 must default to inherit=0.

        Upgrade to the revision before 009, raw-insert a camera row (without
        the new column), then apply 009.  The server_default=text("0") must
        back-fill the existing row with 0 (False).
        """
        # Step 1: upgrade to the revision immediately before this feature's
        # migration so the camera table exists but the new column does not.
        command.upgrade(alembic_cfg, "008_add_ldap_tls_ca_cert")

        engine = create_db_engine(tmp_db_url)
        try:
            # Step 2: raw-insert a camera row without credentials_inherit_default.
            with engine.begin() as conn:
                conn.execute(
                    sa.text(
                        "INSERT INTO camera (name, protocol) VALUES (:name, :proto)"
                    ),
                    {"name": "pre-migration-cam", "proto": "vapix"},
                )

            # Step 3: apply migration 009.
            command.upgrade(alembic_cfg, "009_add_camera_default_credentials")

            # Step 4: the pre-existing row must have credentials_inherit_default=0.
            with engine.connect() as conn:
                value = conn.execute(
                    sa.text(
                        "SELECT credentials_inherit_default FROM camera "
                        "WHERE name = 'pre-migration-cam'"
                    )
                ).scalar_one()
            assert value == 0, (
                f"Expected credentials_inherit_default=0 after migration, got {value!r}"
            )
        finally:
            engine.dispose()


class TestSingletonCheckConstraint:
    """Inserting a CameraDefaultCredentials row with id != 1 must raise (Inv 5)."""

    def test_id_not_1_raises_integrity_error(self, migrated_factory: object) -> None:
        """The CheckConstraint ck_camera_default_credentials_singleton enforces id=1.

        Attempting to INSERT a row with id=2 must raise IntegrityError at flush
        time.  This proves the singleton invariant is enforced at the DB level,
        not just by convention in the service layer.
        """
        with pytest.raises(IntegrityError), session_scope(migrated_factory) as session:  # type: ignore[arg-type]
            bad_row = CameraDefaultCredentials(id=2, enabled=False)
            session.add(bad_row)
            session.flush()
