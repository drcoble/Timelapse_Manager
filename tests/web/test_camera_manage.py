"""Web add/edit camera flows: form fragments and the edit-apply path.

These exercise the admin-gated, CSRF-protected camera form routes end to end
through the running app (a real session cookie + form token), which inspection
alone cannot verify. The capture supervisor is constructed but not started in
the web test settings, so no live probing occurs here.

Address validation runs through the SSRF host-resolution chokepoint: the web
test settings opt the private RFC-1918 ranges in, so ``192.168.x.x`` resolves
clean while loopback and the cloud-metadata address are always rejected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.crypto import decrypt_credentials, is_encrypted

# An address inside the test settings' allowed private subnets (192.168.0.0/16).
_ALLOWED_ADDRESS = "192.168.1.50"
# The cloud-metadata address: always denied by the SSRF guard.
_METADATA_ADDRESS = "169.254.169.254"


def _seed_camera(
    *,
    name: str,
    protocol: str | None = "vapix",
    address: str | None = _ALLOWED_ADDRESS,
    credentials: dict[str, object] | None = None,
) -> int:
    """Insert a camera (optionally with encrypted credentials) and return its id."""
    from timelapse_manager.security.crypto import encrypt_credentials

    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(
            name=name,
            address=address,
            protocol=protocol,
            credentials=encrypt_credentials(credentials),
        )
        db.add(cam)
        db.flush()
        return cam.id


def _camera(camera_id: int) -> Camera | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return db.get(Camera, camera_id)


class TestCameraAddForm:
    def test_add_form_returns_200_not_405(self, admin_client: TestClient) -> None:
        # The historical defect: ``GET /cameras/add-form`` was shadowed onto
        # ``DELETE /cameras/{camera_id}`` and 405'd. It must now resolve to 200.
        resp = admin_client.get("/cameras/add-form")
        assert resp.status_code == 200
        assert resp.status_code != 405

    def test_add_form_renders_create_form_with_csrf(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/cameras/add-form")
        assert resp.status_code == 200
        assert 'action="/cameras"' in resp.text
        assert 'name="csrf_token"' in resp.text

    def test_add_form_forbidden_for_viewer(self, viewer_client: TestClient) -> None:
        resp = viewer_client.get("/cameras/add-form", follow_redirects=False)
        assert resp.status_code == 403


class TestCameraFormLayout:
    """The form is address-first with an Advanced disclosure for rare fields."""

    # Every field the form has ever carried; all must survive the restructure
    # so create/edit submits and the Query "Accept all" wiring keep working.
    _FIELD_NAMES = (
        "name",
        "protocol",
        "address",
        "device_hostname",
        "device_hostname_source",
        "snapshot_uri",
        "stream_uri",
        "credentials_inherit_default",
        "username",
        "password",
        "latitude",
        "longitude",
        "geo_source",
    )

    def test_add_form_is_address_first(self, admin_client: TestClient) -> None:
        text = admin_client.get("/cameras/add-form").text
        # Address must appear before Name in source order (the golden path leads
        # with the address + Query, then asks for a name).
        assert 'name="address"' in text
        assert 'name="name"' in text
        assert text.index('name="address"') < text.index('name="name"')

    def test_add_form_query_button_precedes_name(
        self, admin_client: TestClient
    ) -> None:
        text = admin_client.get("/cameras/add-form").text
        # The Query action is part of the address-first golden path, above Name.
        assert 'hx-post="/cameras/query"' in text
        assert text.index('hx-post="/cameras/query"') < text.index('name="name"')

    def test_add_form_has_advanced_disclosure(self, admin_client: TestClient) -> None:
        text = admin_client.get("/cameras/add-form").text
        assert "<details" in text
        summary = text[text.index("<summary") : text.index("</summary>")]
        assert "Advanced" in summary

    def test_advanced_wraps_uris_and_location(self, admin_client: TestClient) -> None:
        text = admin_client.get("/cameras/add-form").text
        start = text.index("<details")
        end = text.index("</details>")
        advanced = text[start:end]
        # The less-common fields live inside the Advanced disclosure.
        assert 'name="snapshot_uri"' in advanced
        assert 'name="stream_uri"' in advanced
        assert 'name="latitude"' in advanced
        assert 'name="longitude"' in advanced
        assert 'name="geo_source"' in advanced

    def test_add_form_keeps_every_field_name(self, admin_client: TestClient) -> None:
        text = admin_client.get("/cameras/add-form").text
        for field in self._FIELD_NAMES:
            assert f'name="{field}"' in text, f"missing field name: {field}"

    def test_inherit_default_checkbox_pre_checked(
        self, admin_client: TestClient
    ) -> None:
        text = admin_client.get("/cameras/add-form").text
        import re

        m = re.search(
            r'<input[^>]*name="credentials_inherit_default"[^>]*>', text, re.DOTALL
        )
        assert m is not None
        assert "checked" in m.group(0)

    def test_edit_form_is_address_first_and_prefilled(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="layout-edit-cam")
        text = admin_client.get(f"/cameras/{camera_id}/edit-form").text
        # Same address-first ordering in edit mode, and still prefilled.
        assert text.index('name="address"') < text.index('name="name"')
        assert "layout-edit-cam" in text
        assert _ALLOWED_ADDRESS in text
        # The edit fragment is still the row (the apply target survives the swap).
        assert f'id="camera-row-{camera_id}"' in text
        # Advanced disclosure present in edit mode too.
        assert "<details" in text


class TestCameraEditForm:
    def test_edit_form_prefilled_password_not_echoed(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(
            name="edit-cam",
            credentials={"username": "operator", "password": "s3cr3t-pass"},
        )
        resp = admin_client.get(f"/cameras/{camera_id}/edit-form")
        assert resp.status_code == 200
        # The fragment IS the row (same id), so the apply's hx-target survives
        # the edit-form swap. A card-shaped fragment would orphan that target.
        assert f'id="camera-row-{camera_id}"' in resp.text
        # Prefilled with the current values.
        assert "edit-cam" in resp.text
        assert _ALLOWED_ADDRESS in resp.text
        assert "operator" in resp.text
        # The password is NEVER rendered back, in plaintext or ciphertext.
        assert "s3cr3t-pass" not in resp.text
        assert "enc:v1:" not in resp.text

    def test_edit_form_unknown_camera_is_404(self, admin_client: TestClient) -> None:
        resp = admin_client.get("/cameras/999999/edit-form")
        assert resp.status_code == 404

    def test_edit_form_forbidden_for_viewer(self, viewer_client: TestClient) -> None:
        camera_id = _seed_camera(name="viewer-editform-cam")
        resp = viewer_client.get(
            f"/cameras/{camera_id}/edit-form", follow_redirects=False
        )
        assert resp.status_code == 403


class TestCameraEditApply:
    def test_rename_persists_and_returns_row(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="before-name")
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "after-name",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        # The success response is the refreshed row fragment.
        assert f'id="camera-row-{camera_id}"' in resp.text
        assert "after-name" in resp.text
        camera = _camera(camera_id)
        assert camera is not None
        assert camera.name == "after-name"

    def test_blank_password_preserves_credentials(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(
            name="cred-cam",
            credentials={"username": "operator", "password": "keep-me-pass"},
        )
        before = _camera(camera_id)
        assert before is not None
        stored_password = (before.credentials or {})["password"]
        assert is_encrypted(str(stored_password))

        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "cred-cam",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "username": "operator",
                "password": "",  # blank: must preserve the stored credential
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        after = _camera(camera_id)
        assert after is not None
        creds = after.credentials or {}
        # Ciphertext is unchanged (not re-encrypted) and still decrypts.
        assert creds["password"] == stored_password
        decrypted = decrypt_credentials(creds) or {}
        assert decrypted["password"] == "keep-me-pass"

    def test_new_password_replaces_credentials(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(
            name="newpw-cam",
            credentials={"username": "operator", "password": "old-pass"},
        )
        before = _camera(camera_id)
        assert before is not None
        old_ciphertext = (before.credentials or {})["password"]

        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "newpw-cam",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "username": "operator",
                "password": "brand-new-pass",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        after = _camera(camera_id)
        assert after is not None
        creds = after.credentials or {}
        assert creds["password"] != old_ciphertext
        assert is_encrypted(str(creds["password"]))
        decrypted = decrypt_credentials(creds) or {}
        assert decrypted["password"] == "brand-new-pass"

    def test_unrelated_fields_preserved(self, admin_client: TestClient) -> None:
        # Fields not on the form (snapshot_uri/stream_uri/geo) must survive edit.
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = Camera(
                name="full-cam",
                address=_ALLOWED_ADDRESS,
                protocol="vapix",
                snapshot_uri="http://192.168.1.50/snap",
                stream_uri="rtsp://192.168.1.50/stream",
                geolocation_latitude=40.0,
                geolocation_longitude=-74.0,
            )
            db.add(cam)
            db.flush()
            camera_id = cam.id

        csrf = csrf_of(admin_client, "/cameras")
        admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "full-cam-renamed",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        after = _camera(camera_id)
        assert after is not None
        assert after.snapshot_uri == "http://192.168.1.50/snap"
        assert after.stream_uri == "rtsp://192.168.1.50/stream"
        assert after.geolocation_latitude == 40.0
        assert after.geolocation_longitude == -74.0

    def test_ssrf_blocked_address_is_rejected(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="ssrf-cam", address=_ALLOWED_ADDRESS)
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "ssrf-cam",
                "protocol": "vapix",
                "address": _METADATA_ADDRESS,  # cloud metadata: always denied
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        # Inline error fragment at 200 (HTMX swaps only successful responses).
        assert resp.status_code == 200
        assert "rejected" in resp.text.lower()
        after = _camera(camera_id)
        assert after is not None
        # The denied address was not stored.
        assert after.address == _ALLOWED_ADDRESS

    def test_empty_name_is_rejected(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="named-cam")
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "   ",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "required" in resp.text.lower()
        after = _camera(camera_id)
        assert after is not None
        assert after.name == "named-cam"

    def test_duplicate_name_is_rejected(self, admin_client: TestClient) -> None:
        _seed_camera(name="taken-name")
        camera_id = _seed_camera(name="other-name")
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "taken-name",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "already exists" in resp.text.lower()
        after = _camera(camera_id)
        assert after is not None
        assert after.name == "other-name"

    def test_viewer_cannot_edit(self, viewer_client: TestClient) -> None:
        camera_id = _seed_camera(name="viewer-noedit-cam")
        csrf = csrf_of(viewer_client, "/cameras")
        resp = viewer_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "hacked",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_missing_csrf_is_forbidden(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="csrf-cam")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "no-csrf",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403
        after = _camera(camera_id)
        assert after is not None
        assert after.name == "csrf-cam"


def _detection_outcome() -> object:
    """A DetectionOutcome with two responders (VAPIX recommended) + one miss."""
    from timelapse_manager.cameras.probing import (
        Confidence,
        DetectionOutcome,
        ProtocolCandidate,
    )

    return DetectionOutcome(
        candidates=[
            ProtocolCandidate(
                protocol="vapix",
                ok=True,
                snapshot_uri="http://192.168.1.50/axis-cgi/jpg/image.cgi",
                confidence=Confidence.HIGH,
                detail="Axis VAPIX snapshot CGI responded with an image.",
            ),
            ProtocolCandidate(
                protocol="onvif",
                ok=True,
                snapshot_uri="http://192.168.1.50/onvif/snap",
                stream_uri="rtsp://192.168.1.50/onvif/stream",
                confidence=Confidence.HIGH,
                detail="ONVIF media service returned a media profile.",
            ),
            ProtocolCandidate(
                protocol="rtsp",
                ok=False,
                confidence=Confidence.LOW,
                detail="RTSP control port 554 not reachable.",
            ),
            ProtocolCandidate(
                protocol="http",
                ok=False,
                confidence=Confidence.LOW,
                detail="No common HTTP snapshot path returned an image.",
            ),
        ],
        recommended_primary="vapix",
    )


class TestCameraDetectProtocol:
    def test_form_has_snapshot_stream_inputs_and_query_button(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get("/cameras/add-form")
        assert resp.status_code == 200
        assert 'name="snapshot_uri"' in resp.text
        assert 'name="stream_uri"' in resp.text
        # The form's primary probe action is now the consolidated Query camera
        # button posting to the query route, with its results container.
        assert 'hx-post="/cameras/query"' in resp.text
        assert 'id="camera-query-results"' in resp.text

    def test_multi_candidate_fragment_renders_indicator_and_recommended(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/cameras")
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch(
                "timelapse_manager.cameras.detect_protocols",
                AsyncMock(return_value=_detection_outcome()),
            ),
        ):
            resp = admin_client.post(
                "/cameras/detect-protocol",
                data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        text = resp.text
        # The ">1 detected" indicator banner.
        assert "2 protocols detected" in text
        # Both responders rendered as radios; the misses are not selectable.
        assert 'value="vapix"' in text
        assert 'value="onvif"' in text
        # The recommended primary is the pre-selected radio.
        import re

        recommended = re.search(r'<input[^>]*value="vapix"[^>]*>', text, re.DOTALL)
        assert recommended is not None
        assert "checked" in recommended.group(0)

    def test_detect_blocks_denied_address_without_probing(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/cameras")
        with patch("timelapse_manager.cameras.detect_protocols") as mock_detect:
            resp = admin_client.post(
                "/cameras/detect-protocol",
                data={"address": _METADATA_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        # Fragment swaps on 200; the error is rendered, and no probe ran.
        assert resp.status_code == 200
        assert "rejected" in resp.text.lower()
        mock_detect.assert_not_called()

    def test_detect_forbidden_for_viewer(self, viewer_client: TestClient) -> None:
        csrf = csrf_of(viewer_client, "/cameras")
        resp = viewer_client.post(
            "/cameras/detect-protocol",
            data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403

    def test_detect_fragment_never_echoes_credentials(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/cameras")
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch(
                "timelapse_manager.cameras.detect_protocols",
                AsyncMock(return_value=_detection_outcome()),
            ),
        ):
            resp = admin_client.post(
                "/cameras/detect-protocol",
                data={
                    "address": _ALLOWED_ADDRESS,
                    "username": "joe",
                    "password": "sup3r-secret",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        assert "sup3r-secret" not in resp.text

    def test_blank_credentials_substitute_saved_default(
        self, admin_client: TestClient
    ) -> None:
        """Blank form creds → the saved global default reaches detect_protocols."""
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context
        from timelapse_manager.security.camera_defaults_service import (
            CameraDefaultsUpdate,
            update_settings,
        )

        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            update_settings(
                db,
                CameraDefaultsUpdate(
                    enabled=True, username="default-joe", password="default-pass"
                ),
            )

        csrf = csrf_of(admin_client, "/cameras")
        mock_detect = AsyncMock(return_value=_detection_outcome())
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch("timelapse_manager.cameras.detect_protocols", mock_detect),
        ):
            resp = admin_client.post(
                "/cameras/detect-protocol",
                data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        assert mock_detect.await_args.args[1] == ("default-joe", "default-pass")

    def test_detect_allowed_for_operator(self, operator_client: TestClient) -> None:
        """Operator role (not just admin) must be permitted to use detect."""
        csrf = csrf_of(operator_client, "/cameras")
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch(
                "timelapse_manager.cameras.detect_protocols",
                AsyncMock(return_value=_detection_outcome()),
            ),
        ):
            resp = operator_client.post(
                "/cameras/detect-protocol",
                data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200

    def test_supplied_credentials_reach_detect_protocols(
        self, admin_client: TestClient
    ) -> None:
        """When form supplies username+password, that pair reaches detect_protocols.

        Distinct from the blank-creds test: this proves the supplied pair is NOT
        replaced by the default when credentials are explicitly given.
        """
        csrf = csrf_of(admin_client, "/cameras")
        mock_detect = AsyncMock(return_value=_detection_outcome())
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch("timelapse_manager.cameras.detect_protocols", mock_detect),
        ):
            resp = admin_client.post(
                "/cameras/detect-protocol",
                data={
                    "address": _ALLOWED_ADDRESS,
                    "username": "form-user",
                    "password": "form-pass",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        # The credentials passed to detect_protocols must be exactly what the form
        # supplied -- not the saved default.
        assert mock_detect.called
        passed_credentials = mock_detect.await_args.args[1]
        assert passed_credentials == ("form-user", "form-pass")

    def test_single_candidate_fragment_shows_one_detected_not_banner(
        self, admin_client: TestClient
    ) -> None:
        """Exactly one responder renders '1 protocol detected', not the >1 banner."""
        from timelapse_manager.cameras.probing import (
            Confidence,
            DetectionOutcome,
            ProtocolCandidate,
        )

        single_outcome = DetectionOutcome(
            candidates=[
                ProtocolCandidate(
                    protocol="vapix",
                    ok=True,
                    snapshot_uri="http://192.168.1.50/axis-cgi/jpg/image.cgi",
                    confidence=Confidence.HIGH,
                    detail="Axis VAPIX snapshot CGI responded with an image.",
                ),
                ProtocolCandidate(
                    protocol="onvif", ok=False, confidence=Confidence.HIGH, detail="no"
                ),
                ProtocolCandidate(
                    protocol="rtsp", ok=False, confidence=Confidence.LOW, detail="no"
                ),
                ProtocolCandidate(
                    protocol="http", ok=False, confidence=Confidence.LOW, detail="no"
                ),
            ],
            recommended_primary="vapix",
        )
        csrf = csrf_of(admin_client, "/cameras")
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch(
                "timelapse_manager.cameras.detect_protocols",
                AsyncMock(return_value=single_outcome),
            ),
        ):
            resp = admin_client.post(
                "/cameras/detect-protocol",
                data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        text = resp.text
        # Single-responder message; NOT the multi-responder banner.
        assert "1 protocol detected" in text
        assert "2 protocols detected" not in text
        # The single ok candidate is rendered as a checked radio.
        assert 'value="vapix"' in text
        assert "checked" in text
        # The non-ok candidates must not appear as selectable radios.
        assert 'value="onvif"' not in text

    def test_zero_candidates_fragment_shows_nothing_detected(
        self, admin_client: TestClient
    ) -> None:
        """When no protocol responds the fragment shows the 'nothing detected' state."""
        from timelapse_manager.cameras.probing import (
            Confidence,
            DetectionOutcome,
            ProtocolCandidate,
        )

        zero_outcome = DetectionOutcome(
            candidates=[
                ProtocolCandidate(
                    protocol="vapix", ok=False, confidence=Confidence.HIGH, detail="no"
                ),
                ProtocolCandidate(
                    protocol="onvif", ok=False, confidence=Confidence.HIGH, detail="no"
                ),
                ProtocolCandidate(
                    protocol="rtsp", ok=False, confidence=Confidence.LOW, detail="no"
                ),
                ProtocolCandidate(
                    protocol="http", ok=False, confidence=Confidence.LOW, detail="no"
                ),
            ],
            recommended_primary=None,
        )
        csrf = csrf_of(admin_client, "/cameras")
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch(
                "timelapse_manager.cameras.detect_protocols",
                AsyncMock(return_value=zero_outcome),
            ),
        ):
            resp = admin_client.post(
                "/cameras/detect-protocol",
                data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        text = resp.text
        # The "nothing responded" fallback branch must be shown.
        assert "no protocols responded" in text.lower()
        # No radio inputs should appear (nothing to select).
        assert 'type="radio"' not in text

    def test_detect_fragment_never_echoes_credentials_in_uri(
        self, admin_client: TestClient
    ) -> None:
        """A mock outcome whose URIs contain no credentials: verify the fragment too.

        The companion invariant-2 unit tests (TestNoCredentialInUri) exercise the
        real probe helpers. This web-layer test verifies the fragment itself when
        the mock outcome supplies clean URIs -- ensuring the template does not add
        any credential-containing suffix and that the full rendered text contains
        neither the username nor the password sent in the form post.
        """
        from timelapse_manager.cameras.probing import (
            Confidence,
            DetectionOutcome,
            ProtocolCandidate,
        )

        outcome_with_uris = DetectionOutcome(
            candidates=[
                ProtocolCandidate(
                    protocol="vapix",
                    ok=True,
                    snapshot_uri="http://192.168.1.50/axis-cgi/jpg/image.cgi",
                    confidence=Confidence.HIGH,
                    detail="ok",
                ),
                ProtocolCandidate(
                    protocol="onvif",
                    ok=True,
                    snapshot_uri="http://192.168.1.50/onvif/snap",
                    stream_uri="rtsp://192.168.1.50/onvif/stream",
                    confidence=Confidence.HIGH,
                    detail="ok",
                ),
                ProtocolCandidate(
                    protocol="rtsp", ok=False, confidence=Confidence.LOW, detail="no"
                ),
                ProtocolCandidate(
                    protocol="http", ok=False, confidence=Confidence.LOW, detail="no"
                ),
            ],
            recommended_primary="vapix",
        )

        csrf = csrf_of(admin_client, "/cameras")
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch(
                "timelapse_manager.cameras.detect_protocols",
                AsyncMock(return_value=outcome_with_uris),
            ),
        ):
            resp = admin_client.post(
                "/cameras/detect-protocol",
                data={
                    "address": _ALLOWED_ADDRESS,
                    "username": "cam-user",
                    "password": "cam-secret",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        text = resp.text
        # Neither credential must appear anywhere in the rendered fragment.
        assert "cam-secret" not in text
        assert "cam-user" not in text


def _query_result(
    *,
    ok: bool = True,
    discovered_hostname: str | None = "lobby-cam",
    fetched_lat: float | None = 47.6,
    fetched_lon: float | None = -122.3,
    error_protocol: str | None = None,
    error_hostname: str | None = None,
    error_geo: str | None = None,
    auth_rejected: bool = False,
) -> object:
    """Build a QueryResult for the consolidated camera-query panel.

    ``ok=True`` yields a single VAPIX responder plus a hostname and location; the
    error_* fields are filled in for partial/hard-failure variants.
    """
    from timelapse_manager.cameras.autoquery import QueryResult
    from timelapse_manager.cameras.probing import Confidence, ProtocolCandidate

    candidates = (
        [
            ProtocolCandidate(
                protocol="vapix",
                ok=True,
                snapshot_uri="http://192.168.1.50/axis-cgi/jpg/image.cgi",
                confidence=Confidence.HIGH,
                detail="Axis VAPIX snapshot CGI responded with an image.",
            )
        ]
        if ok
        else []
    )
    return QueryResult(
        candidates=candidates,
        recommended_primary="vapix" if ok else None,
        ok_count=1 if ok else 0,
        discovered_hostname=discovered_hostname,
        fetched_lat=fetched_lat,
        fetched_lon=fetched_lon,
        error_protocol=error_protocol,
        error_hostname=error_hostname,
        error_geo=error_geo,
        auth_rejected=auth_rejected,
    )


class TestCameraQuery:
    """The consolidated POST /cameras/query panel."""

    def test_panel_renders_all_three_sections(self, admin_client: TestClient) -> None:
        csrf = csrf_of(admin_client, "/cameras")
        mock_query = AsyncMock(return_value=_query_result())
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch("timelapse_manager.cameras.autoquery.query_camera", mock_query),
        ):
            resp = admin_client.post(
                "/cameras/query",
                data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        text = resp.text
        # Protocol radio for the responder, the discovered hostname + apply
        # affordance, and the reported coordinates + apply affordance.
        assert 'value="vapix"' in text
        assert "lobby-cam" in text
        assert "Use discovered hostname" in text
        assert "47.6" in text and "-122.3" in text
        assert "Use these coordinates" in text

    def test_partial_no_location_does_not_blank_protocol(
        self, admin_client: TestClient
    ) -> None:
        """A protocol responds but no location: location degrades to a hint only."""
        csrf = csrf_of(admin_client, "/cameras")
        mock_query = AsyncMock(
            return_value=_query_result(
                fetched_lat=None, fetched_lon=None, error_geo="no_location"
            )
        )
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch("timelapse_manager.cameras.autoquery.query_camera", mock_query),
        ):
            resp = admin_client.post(
                "/cameras/query",
                data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        text = resp.text
        # Protocol is still surfaced; the location section is a hint, not an error.
        assert 'value="vapix"' in text
        assert "did not report a location" in text
        assert "Use these coordinates" not in text

    def test_masked_auth_rejection_shows_credential_warning(
        self, admin_client: TestClient
    ) -> None:
        """A responder plus a masked auth rejection surfaces a credential warning."""
        csrf = csrf_of(admin_client, "/cameras")
        mock_query = AsyncMock(return_value=_query_result(auth_rejected=True))
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch("timelapse_manager.cameras.autoquery.query_camera", mock_query),
        ):
            resp = admin_client.post(
                "/cameras/query",
                data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        text = resp.text
        # The responder is still offered, but the credential-rejected warning shows.
        assert 'value="vapix"' in text
        assert "rejected these" in text
        assert "check the username and password" in text

    def test_hard_failure_shows_error(self, admin_client: TestClient) -> None:
        csrf = csrf_of(admin_client, "/cameras")
        mock_query = AsyncMock(
            return_value=_query_result(
                ok=False,
                discovered_hostname=None,
                fetched_lat=None,
                fetched_lon=None,
                error_protocol="auth_failed",
                error_hostname="no_hostname",
                error_geo="unreachable",
            )
        )
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch("timelapse_manager.cameras.autoquery.query_camera", mock_query),
        ):
            resp = admin_client.post(
                "/cameras/query",
                data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        assert "rejected the credentials" in resp.text

    def test_query_blocks_denied_address_without_probing(
        self, admin_client: TestClient
    ) -> None:
        csrf = csrf_of(admin_client, "/cameras")
        with patch("timelapse_manager.cameras.autoquery.query_camera") as mock_query:
            resp = admin_client.post(
                "/cameras/query",
                data={"address": _METADATA_ADDRESS, "csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        assert "rejected" in resp.text.lower()
        mock_query.assert_not_called()

    def test_query_never_echoes_credentials(self, admin_client: TestClient) -> None:
        csrf = csrf_of(admin_client, "/cameras")
        mock_query = AsyncMock(return_value=_query_result())
        with (
            patch(
                "timelapse_manager.cameras.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch("timelapse_manager.cameras.autoquery.query_camera", mock_query),
        ):
            resp = admin_client.post(
                "/cameras/query",
                data={
                    "address": _ALLOWED_ADDRESS,
                    "username": "joe",
                    "password": "sup3r-secret",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        assert "sup3r-secret" not in resp.text
        # The form's own credentials reach query_camera as a dict; the global
        # default is passed separately as default_credentials.
        assert mock_query.await_args.kwargs["credentials"] == {
            "username": "joe",
            "password": "sup3r-secret",
        }

    def test_query_forbidden_for_viewer(self, viewer_client: TestClient) -> None:
        csrf = csrf_of(viewer_client, "/cameras")
        resp = viewer_client.post(
            "/cameras/query",
            data={"address": _ALLOWED_ADDRESS, "csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 403


class TestCameraDeviceHostname:
    """device_hostname persists on create/edit with its source."""

    def test_create_persists_hostname_with_camera_source(
        self, admin_client: TestClient
    ) -> None:
        from sqlalchemy import select

        csrf = csrf_of(admin_client, "/cameras")
        with patch(
            "timelapse_manager.cameras.resolve_camera_host",
            side_effect=lambda a: a,
        ):
            resp = admin_client.post(
                "/cameras",
                data={
                    "name": "host-cam",
                    "protocol": "vapix",
                    "address": _ALLOWED_ADDRESS,
                    "device_hostname": "lobby-cam",
                    "device_hostname_source": "camera",
                    "credentials_inherit_default": "on",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = db.execute(
                select(Camera).where(Camera.name == "host-cam")
            ).scalar_one()
            assert cam.device_hostname == "lobby-cam"
            assert cam.device_hostname_source == "camera"

    def test_create_typed_hostname_defaults_to_manual_source(
        self, admin_client: TestClient
    ) -> None:
        from sqlalchemy import select

        csrf = csrf_of(admin_client, "/cameras")
        with patch(
            "timelapse_manager.cameras.resolve_camera_host",
            side_effect=lambda a: a,
        ):
            resp = admin_client.post(
                "/cameras",
                data={
                    "name": "typed-host-cam",
                    "protocol": "vapix",
                    "address": _ALLOWED_ADDRESS,
                    "device_hostname": "typed-name",
                    "credentials_inherit_default": "on",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = db.execute(
                select(Camera).where(Camera.name == "typed-host-cam")
            ).scalar_one()
            assert cam.device_hostname == "typed-name"
            assert cam.device_hostname_source == "manual"

    def test_edit_updates_hostname_and_camera_source(
        self, admin_client: TestClient
    ) -> None:
        # The address is unchanged, so the edit path skips SSRF re-resolution; a
        # supplied hostname plus a "camera" source (the value the apply button
        # sets) is persisted.
        camera_id = _seed_camera(name="update-host-cam")
        csrf = csrf_of(admin_client, "/cameras")
        resp = admin_client.post(
            f"/cameras/{camera_id}/edit",
            data={
                "name": "update-host-cam",
                "protocol": "vapix",
                "address": _ALLOWED_ADDRESS,
                "device_hostname": "discovered-host",
                "device_hostname_source": "camera",
                "csrf_token": csrf,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        after = _camera(camera_id)
        assert after is not None
        assert after.device_hostname == "discovered-host"
        assert after.device_hostname_source == "camera"

    def test_edit_form_prefills_hostname(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="prefill-host-cam")
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            cam = db.get(Camera, camera_id)
            assert cam is not None
            cam.device_hostname = "prefilled-host"
            cam.device_hostname_source = "camera"
        resp = admin_client.get(f"/cameras/{camera_id}/edit-form")
        assert resp.status_code == 200
        assert 'name="device_hostname"' in resp.text
        assert "prefilled-host" in resp.text


class TestCameraDiscoverFragment:
    def test_discover_fragment_shows_resolved_uri(
        self, operator_client: TestClient
    ) -> None:
        """A resolved snapshot/stream URI is shown in the scan-results fragment."""
        from timelapse_manager.cameras.base import DiscoveredCamera

        csrf = csrf_of(operator_client, "/cameras")
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
            patch(
                "timelapse_manager.cameras.discover_onvif",
                new=AsyncMock(return_value=found),
            ),
            patch(
                "timelapse_manager.cameras.discovery.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch(
                "timelapse_manager.cameras.discovery.OnvifAdapter.resolve_uris",
                new=AsyncMock(
                    return_value=(
                        "http://192.168.1.50/snap.jpg",
                        "rtsp://192.168.1.50/stream",
                    )
                ),
            ),
            patch(
                "timelapse_manager.cameras.discovery.OnvifAdapter.close",
                new=AsyncMock(),
            ),
        ):
            resp = operator_client.post(
                "/cameras/discover",
                data={"csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        text = resp.text
        assert _ALLOWED_ADDRESS in text
        assert "snapshot: http://192.168.1.50/snap.jpg" in text
        assert "stream: rtsp://192.168.1.50/stream" in text

    def test_discover_fragment_no_cameras(self, operator_client: TestClient) -> None:
        """The empty branch still renders the 'No cameras found.' message."""
        csrf = csrf_of(operator_client, "/cameras")
        with patch(
            "timelapse_manager.cameras.discover_onvif",
            new=AsyncMock(return_value=[]),
        ):
            resp = operator_client.post(
                "/cameras/discover",
                data={"csrf_token": csrf},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200
        assert "No cameras found." in resp.text

    def test_blank_range_triggers_multicast_and_keeps_enrichment(
        self, operator_client: TestClient
    ) -> None:
        """A blank scan_range scans the LAN (multicast) and still enriches URIs."""
        from timelapse_manager.cameras.base import DiscoveredCamera

        csrf = csrf_of(operator_client, "/cameras")
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
        multicast = AsyncMock(return_value=found)
        scan = AsyncMock(return_value=[])
        with (
            patch("timelapse_manager.cameras.discover_onvif", new=multicast),
            patch("timelapse_manager.cameras.scan_range", new=scan),
            patch(
                "timelapse_manager.cameras.discovery.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch(
                "timelapse_manager.cameras.discovery.OnvifAdapter.resolve_uris",
                new=AsyncMock(
                    return_value=(
                        "http://192.168.1.50/snap.jpg",
                        "rtsp://192.168.1.50/stream",
                    )
                ),
            ),
            patch(
                "timelapse_manager.cameras.discovery.OnvifAdapter.close",
                new=AsyncMock(),
            ),
        ):
            resp = operator_client.post(
                "/cameras/discover",
                data={"csrf_token": csrf, "scan_range": ""},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        # The multicast path ran; the range scanner did not.
        multicast.assert_awaited_once()
        scan.assert_not_called()
        # Existing default-credential URI enrichment is preserved.
        assert "snapshot: http://192.168.1.50/snap.jpg" in resp.text
        assert "stream: rtsp://192.168.1.50/stream" in resp.text

    def test_valid_cidr_lists_discovered_cameras(
        self, operator_client: TestClient
    ) -> None:
        """A valid CIDR runs the range scan and lists the discovered camera."""
        from timelapse_manager.cameras.base import DiscoveredCamera

        csrf = csrf_of(operator_client, "/cameras")
        found = [
            DiscoveredCamera(
                address="192.168.1.77",
                protocol="onvif",
                snapshot_uri=None,
                stream_uri=None,
                geolocation=None,
                vendor=None,
            )
        ]
        scan = AsyncMock(return_value=found)
        multicast = AsyncMock(return_value=[])
        with (
            patch("timelapse_manager.cameras.scan_range", new=scan),
            patch("timelapse_manager.cameras.discover_onvif", new=multicast),
            patch(
                "timelapse_manager.cameras.discovery.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch(
                "timelapse_manager.cameras.discovery.OnvifAdapter.resolve_uris",
                new=AsyncMock(return_value=(None, None)),
            ),
            patch(
                "timelapse_manager.cameras.discovery.OnvifAdapter.close",
                new=AsyncMock(),
            ),
        ):
            resp = operator_client.post(
                "/cameras/discover",
                data={"csrf_token": csrf, "scan_range": "192.168.1.0/24"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        scan.assert_awaited_once_with("192.168.1.0/24")
        multicast.assert_not_called()
        assert "192.168.1.77" in resp.text

    def test_malformed_range_returns_error_and_does_not_scan(
        self, operator_client: TestClient
    ) -> None:
        """A bare/garbage range yields an .alert error at 200 and never scans."""
        csrf = csrf_of(operator_client, "/cameras")
        scan = AsyncMock(return_value=[])
        multicast = AsyncMock(return_value=[])
        with (
            patch("timelapse_manager.cameras.scan_range", new=scan),
            patch("timelapse_manager.cameras.discover_onvif", new=multicast),
        ):
            resp = operator_client.post(
                "/cameras/discover",
                data={"csrf_token": csrf, "scan_range": "192.168.1"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        assert 'class="alert error"' in resp.text
        assert "not a valid range" in resp.text
        scan.assert_not_called()
        multicast.assert_not_called()

    def test_over_cap_range_returns_warning_and_does_not_scan(
        self, operator_client: TestClient
    ) -> None:
        """A /16 (over the 1024 cap) yields an .alert warning and never scans."""
        csrf = csrf_of(operator_client, "/cameras")
        scan = AsyncMock(return_value=[])
        multicast = AsyncMock(return_value=[])
        with (
            patch("timelapse_manager.cameras.scan_range", new=scan),
            patch("timelapse_manager.cameras.discover_onvif", new=multicast),
        ):
            resp = operator_client.post(
                "/cameras/discover",
                data={"csrf_token": csrf, "scan_range": "10.0.0.0/16"},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        assert resp.status_code == 200, resp.text
        assert 'class="alert warning"' in resp.text
        # The host count and the cap are both stated; the range is not truncated.
        assert "over the limit of 1024" in resp.text
        scan.assert_not_called()
        multicast.assert_not_called()


class TestCameraUriPersistence:
    def test_create_persists_snapshot_and_stream_uri(
        self, admin_client: TestClient
    ) -> None:
        from timelapse_manager.db.models import Camera
        from timelapse_manager.db.session import session_scope
        from timelapse_manager.runtime import get_context

        csrf = csrf_of(admin_client, "/cameras")
        with patch(
            "timelapse_manager.cameras.resolve_camera_host",
            side_effect=lambda a: a,
        ):
            resp = admin_client.post(
                "/cameras",
                data={
                    "name": "uri-cam",
                    "protocol": "vapix",
                    "address": _ALLOWED_ADDRESS,
                    "snapshot_uri": "http://192.168.1.50/snap.jpg",
                    "stream_uri": "rtsp://192.168.1.50/stream",
                    "credentials_inherit_default": "on",
                    "csrf_token": csrf,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                follow_redirects=False,
            )
        assert resp.status_code == 303
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            from sqlalchemy import select

            cam = db.execute(
                select(Camera).where(Camera.name == "uri-cam")
            ).scalar_one()
            assert cam.snapshot_uri == "http://192.168.1.50/snap.jpg"
            assert cam.stream_uri == "rtsp://192.168.1.50/stream"
