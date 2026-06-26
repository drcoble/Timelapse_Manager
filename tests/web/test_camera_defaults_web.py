"""Web-layer tests for the default-camera-credentials settings panel and the
per-camera inherit flag.

Covers:
- Admin-only gate: non-admin roles get 403 on the settings POST.
- Settings round-trip: fields save and the password is masked on re-read; a blank
  password keeps the stored secret unchanged and is never echoed to the page.
- Per-camera flag: create and edit persist ``credentials_inherit_default``; the
  edit form never renders a password value.
"""

from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.cameras.http_jpeg import credentials_from
from timelapse_manager.db.models import Camera, CameraDefaultCredentials
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.camera_defaults_service import MASK_SENTINEL
from timelapse_manager.security.crypto import encrypt_credentials

# An address inside the test settings' allowed private subnets (192.168.0.0/16).
_ALLOWED_ADDRESS = "192.168.1.50"


def _post_defaults(
    client: TestClient, data: dict[str, str], *, follow_redirects: bool = False
) -> Any:
    csrf = csrf_of(client, "/settings")
    payload = {"csrf_token": csrf, **data}
    return client.post(
        "/settings/camera-credentials",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        follow_redirects=follow_redirects,
    )


def _stored_password() -> str | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        row = db.get(CameraDefaultCredentials, 1)
        return row.password if row else None


def _camera(camera_id: int) -> Camera | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.get(Camera, camera_id)


def _seed_camera(
    *, name: str, inherit: bool, credentials: dict[str, object] | None = None
) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address=_ALLOWED_ADDRESS,
            protocol="vapix",
            credentials=encrypt_credentials(credentials),
            credentials_inherit_default=inherit,
        )
        db.add(cam)
        db.flush()
        return cam.id


class TestDefaultsAdminOnly:
    def test_viewer_post_is_403(self, viewer_client: TestClient) -> None:
        csrf = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            "/settings/camera-credentials",
            data={"csrf_token": csrf, "camera_defaults_enabled": "on"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_operator_post_is_403(self, operator_client: TestClient) -> None:
        csrf = csrf_of(operator_client, "/")
        resp = operator_client.post(
            "/settings/camera-credentials",
            data={"csrf_token": csrf, "camera_defaults_enabled": "on"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestDefaultsRoundTrip:
    def test_save_then_password_masked_and_not_echoed(
        self, admin_client: TestClient
    ) -> None:
        resp = _post_defaults(
            admin_client,
            {
                "camera_defaults_enabled": "on",
                "camera_defaults_username": "shared-user",
                "camera_defaults_password": "shared-secret",
            },
        )
        assert resp.status_code == 303

        # The stored password is encrypted, never the plaintext.
        stored = _stored_password()
        assert stored is not None
        assert stored.startswith("enc:v1:")
        assert "shared-secret" not in stored

        # The settings page renders the username clear and the password masked.
        page = admin_client.get("/settings")
        assert page.status_code == 200
        assert "shared-user" in page.text
        assert "shared-secret" not in page.text
        assert f'value="{MASK_SENTINEL}"' in page.text

    def test_blank_password_keeps_stored_secret(self, admin_client: TestClient) -> None:
        _post_defaults(
            admin_client,
            {
                "camera_defaults_enabled": "on",
                "camera_defaults_username": "shared-user",
                "camera_defaults_password": "original-secret",
            },
        )
        before = _stored_password()

        # Re-save without retyping the password (the form shows the mask).
        _post_defaults(
            admin_client,
            {
                "camera_defaults_enabled": "on",
                "camera_defaults_username": "shared-user",
                "camera_defaults_password": MASK_SENTINEL,
            },
        )
        after = _stored_password()
        assert after == before  # ciphertext untouched, not re-encrypted


class TestCameraInheritFlag:
    def test_create_persists_flag_on(self, admin_client: TestClient) -> None:
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            "/cameras",
            data={
                "name": "inherit-on-cam",
                "protocol": "vapix",
                # Address omitted: the SSRF chokepoint only runs on a supplied
                # address, and this test asserts the inherit flag persists.
                "credentials_inherit_default": "on",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = db.query(Camera).filter_by(name="inherit-on-cam").one()
            assert cam.credentials_inherit_default is True

    def test_create_persists_flag_off_when_unticked(
        self, admin_client: TestClient
    ) -> None:
        # An unticked checkbox submits nothing, so the flag must land False.
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            "/cameras",
            data={
                "name": "inherit-off-cam",
                "protocol": "vapix",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = db.query(Camera).filter_by(name="inherit-off-cam").one()
            assert cam.credentials_inherit_default is False

    def test_edit_toggles_flag_off(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="toggle-cam", inherit=True)
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "toggle-cam",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                # Checkbox omitted -> unticked -> flag goes False.
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        cam = _camera(camera_id)
        assert cam is not None
        assert cam.credentials_inherit_default is False

    def test_switching_to_inherit_clears_own_credentials(
        self, admin_client: TestClient
    ) -> None:
        # A camera that had its own username, switched to inherit the default.
        # Disabled inputs submit nothing, so the write path must drop the stored
        # own credentials -- otherwise own creds would keep overriding the default.
        camera_id = _seed_camera(
            name="switch-cam",
            inherit=False,
            credentials={"username": "bob", "password": "bob-pass"},
        )
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "switch-cam",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "credentials_inherit_default": "on",
                # username/password omitted: the inputs are disabled while
                # inheriting, so the browser submits nothing for them.
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        cam = _camera(camera_id)
        assert cam is not None
        assert cam.credentials_inherit_default is True
        # No own credentials remain, so the default fallback actually applies.
        assert credentials_from(cam) is None

    def test_edit_form_reflects_stored_flag_and_hides_password(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="reflect-cam", inherit=True)
        resp = admin_client.get(f"/cameras/{camera_id}/edit-form")
        assert resp.status_code == 200
        # The inherit checkbox is checked when the stored flag is on.
        assert "credentials_inherit_default" in resp.text
        assert "checked" in resp.text
        # A password value is never rendered into the edit form.
        assert 'name="password"' in resp.text
        assert f'id="cam-{camera_id}-password"' in resp.text
