"""Integration tests for the /api/v1/cameras endpoints.

Uses the migrated_client fixture (fully-migrated DB, autostart=False) and
cam_auth_token for authentication.

Covers:
- Camera CRUD: create (201), list (200), get (200/404), delete (204/404)
- Auth: 401 without token, 401 with wrong token
- Protocol validation: 400 on unknown protocol
- Validation endpoint: 400 when camera has no protocol configured
- Manual capture endpoint: 200 when project_id matches camera; 400 when project
  does not belong to camera; 404 when camera not found
- Discover endpoint: returns [] in sandbox (mocked to avoid real network)
- Capture status: 200 for known camera

All tests are purely I/O to the local ASGI TestClient; no real network cameras.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from timelapse_manager.cameras.base import (
    CapturedFrame,
    ValidationFailure,
    ValidationResult,
)
from timelapse_manager.capture.frame_writer import WrittenFrame
from timelapse_manager.config.settings import Settings
from timelapse_manager.db.engine import create_db_engine
from timelapse_manager.db.models import Camera, Project
from timelapse_manager.db.session import create_session_factory, session_scope

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

API = "/api/v1/cameras"


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_camera(client: TestClient, token: str, **overrides) -> dict:
    """POST a minimal camera and return the JSON response body."""
    payload = {"name": "test-cam", **overrides}
    resp = client.post(API, json=payload, headers=_auth(token))
    assert resp.status_code == 201, resp.text
    return resp.json()


def _seed_project(settings: Settings, camera_id: int, interval: int = 60) -> int:
    """Directly insert a Project row and return its id."""
    engine = create_db_engine(settings.database.url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            proj = Project(
                camera_id=camera_id,
                name=f"proj-for-cam-{camera_id}",
                capture_interval_seconds=interval,
                lifecycle_state="active",
                operational_status="idle",
            )
            session.add(proj)
            session.flush()
            return proj.id
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestCameraAuth:
    def test_list_cameras_requires_auth(self, migrated_client: TestClient) -> None:
        resp = migrated_client.get(API)
        assert resp.status_code == 401

    def test_wrong_token_returns_401(self, migrated_client: TestClient) -> None:
        resp = migrated_client.get(API, headers={"Authorization": "Bearer wrong-token"})
        assert resp.status_code == 401

    def test_valid_token_accesses_list(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.get(API, headers=_auth(cam_auth_token))
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Camera CRUD
# ---------------------------------------------------------------------------


class TestCameraCreate:
    def test_create_minimal_camera_returns_201(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.post(
            API, json={"name": "minimal-cam"}, headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 201

    def test_create_response_includes_id(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        body = _create_camera(migrated_client, cam_auth_token, name="cam-id-test")
        assert "id" in body
        assert isinstance(body["id"], int)

    def test_create_response_does_not_include_credentials(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        body = _create_camera(
            migrated_client,
            cam_auth_token,
            name="cam-with-creds",
            credentials={"username": "admin", "password": "secret"},
        )
        assert "password" not in body
        assert "credentials" not in body

    def test_create_with_unknown_protocol_returns_400(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.post(
            API,
            json={"name": "bad-protocol-cam", "protocol": "ftp"},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 400

    def test_duplicate_name_is_rejected_by_db(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        # The unique constraint on camera.name is enforced at the DB level.
        # The application currently propagates IntegrityError as an uncaught
        # server exception (no dedicated 409 handler). We verify the constraint
        # holds by asserting the camera count is still 1 after the second insert
        # attempt (regardless of whether the second POST raises or returns 500).
        import contextlib

        _create_camera(migrated_client, cam_auth_token, name="dup-name")
        # The IntegrityError may propagate out of the TestClient as an exception.
        with contextlib.suppress(Exception):  # noqa: BLE001
            migrated_client.post(
                API, json={"name": "dup-name"}, headers=_auth(cam_auth_token)
            )
        # Only one camera with that name should exist
        engine = create_db_engine(settings_no_autostart.database.url)
        factory = create_session_factory(engine)
        try:
            with session_scope(factory) as session:
                count = session.query(Camera).filter_by(name="dup-name").count()
        finally:
            engine.dispose()
        assert count == 1


class TestCameraList:
    def test_empty_list_when_no_cameras(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.get(API, headers=_auth(cam_auth_token))
        assert resp.status_code == 200
        assert resp.json() == []

    def test_created_cameras_appear_in_list(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        _create_camera(migrated_client, cam_auth_token, name="listed-cam-1")
        _create_camera(migrated_client, cam_auth_token, name="listed-cam-2")

        cameras = migrated_client.get(API, headers=_auth(cam_auth_token)).json()
        names = [c["name"] for c in cameras]
        assert "listed-cam-1" in names
        assert "listed-cam-2" in names


class TestCameraGet:
    def test_get_returns_camera_by_id(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        created = _create_camera(migrated_client, cam_auth_token, name="get-cam")
        resp = migrated_client.get(
            f"{API}/{created['id']}", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 200
        assert resp.json()["id"] == created["id"]

    def test_get_nonexistent_camera_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.get(f"{API}/99999", headers=_auth(cam_auth_token))
        assert resp.status_code == 404


class TestCameraDelete:
    def test_delete_returns_204(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        created = _create_camera(migrated_client, cam_auth_token, name="del-cam")
        resp = migrated_client.delete(
            f"{API}/{created['id']}", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 204

    def test_deleted_camera_not_in_list(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        created = _create_camera(migrated_client, cam_auth_token, name="del-cam2")
        migrated_client.delete(f"{API}/{created['id']}", headers=_auth(cam_auth_token))
        cameras = migrated_client.get(API, headers=_auth(cam_auth_token)).json()
        ids = [c["id"] for c in cameras]
        assert created["id"] not in ids

    def test_delete_nonexistent_camera_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.delete(f"{API}/99999", headers=_auth(cam_auth_token))
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Validation endpoint
# ---------------------------------------------------------------------------


class TestCameraValidate:
    def test_validate_camera_with_no_protocol_returns_400(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        created = _create_camera(migrated_client, cam_auth_token, name="no-proto-cam")
        resp = migrated_client.post(
            f"{API}/{created['id']}/validate", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 400

    def test_validate_nonexistent_camera_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.post(
            f"{API}/99999/validate", headers=_auth(cam_auth_token)
        )
        assert resp.status_code == 404

    def test_validate_with_mocked_adapter_returns_ok(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        created = _create_camera(
            migrated_client,
            cam_auth_token,
            name="vapix-cam",
            protocol="vapix",
            address="10.0.0.1",
        )

        mock_result = ValidationResult(ok=True, reason=None, message="connected")
        mock_geo = None

        with (
            patch("timelapse_manager.api.cameras.build_adapter") as mock_build,
        ):
            fake_adapter = AsyncMock()
            fake_adapter.validate_connection = AsyncMock(return_value=mock_result)
            fake_adapter.get_geolocation = AsyncMock(return_value=mock_geo)
            fake_adapter.close = AsyncMock()
            mock_build.return_value = fake_adapter

            resp = migrated_client.post(
                f"{API}/{created['id']}/validate", headers=_auth(cam_auth_token)
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True

    def test_validate_with_mocked_auth_failure_returns_200_with_ok_false(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        created = _create_camera(
            migrated_client,
            cam_auth_token,
            name="auth-fail-cam",
            protocol="vapix",
            address="10.0.0.2",
        )

        mock_result = ValidationResult(
            ok=False,
            reason=ValidationFailure.AUTH,
            message="authentication rejected",
        )

        with patch("timelapse_manager.api.cameras.build_adapter") as mock_build:
            fake_adapter = AsyncMock()
            fake_adapter.validate_connection = AsyncMock(return_value=mock_result)
            fake_adapter.get_geolocation = AsyncMock(return_value=None)
            fake_adapter.close = AsyncMock()
            mock_build.return_value = fake_adapter

            resp = migrated_client.post(
                f"{API}/{created['id']}/validate", headers=_auth(cam_auth_token)
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["reason"] == "auth"


# ---------------------------------------------------------------------------
# Manual capture endpoint
# ---------------------------------------------------------------------------


class TestManualCapture:
    def test_capture_with_valid_project_returns_200(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
        tmp_path,
    ) -> None:
        created = _create_camera(
            migrated_client,
            cam_auth_token,
            name="cap-cam",
            protocol="vapix",
            address="10.0.0.1",
        )
        project_id = _seed_project(settings_no_autostart, created["id"])

        fake_frame = CapturedFrame(
            image_bytes=b"\xff\xd8\xff\xd9",
            width=1,
            height=1,
            format="jpeg",
            captured_at=datetime.now(UTC),
        )
        now = datetime.now(UTC)
        fake_written = WrittenFrame(
            frame_id=1,
            project_id=project_id,
            sequence_index=1,
            file_path="/tmp/00000001.jpg",
            width=1,
            height=1,
            file_size_bytes=4,
            captured_at=now,
        )

        fake_adapter = AsyncMock()
        fake_adapter.capture = AsyncMock(return_value=fake_frame)
        fake_adapter.close = AsyncMock()

        from timelapse_manager.capture.frame_writer import FrameWriter

        with (
            patch(
                "timelapse_manager.api.cameras.build_adapter", return_value=fake_adapter
            ),
            patch.object(FrameWriter, "write", return_value=fake_written),
        ):
            resp = migrated_client.post(
                f"{API}/{created['id']}/capture",
                json={"project_id": project_id},
                headers=_auth(cam_auth_token),
            )

        assert resp.status_code == 200
        body = resp.json()
        assert body["project_id"] == project_id

    def test_capture_with_mismatched_project_returns_400(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        cam_a = _create_camera(
            migrated_client,
            cam_auth_token,
            name="cam-a",
            protocol="vapix",
            address="10.0.0.1",
        )
        cam_b = _create_camera(
            migrated_client,
            cam_auth_token,
            name="cam-b",
            protocol="vapix",
            address="10.0.0.2",
        )
        project_for_b = _seed_project(settings_no_autostart, cam_b["id"])

        resp = migrated_client.post(
            f"{API}/{cam_a['id']}/capture",
            json={"project_id": project_for_b},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 400

    def test_capture_with_nonexistent_camera_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.post(
            f"{API}/99999/capture",
            json={"project_id": 1},
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Discovery endpoint (mocked to avoid real network)
# ---------------------------------------------------------------------------


class TestDiscoverEndpoint:
    def test_discover_returns_empty_list_in_sandbox(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        with (
            patch(
                "timelapse_manager.api.cameras.discover_onvif",
                new=AsyncMock(return_value=[]),
            ),
        ):
            resp = migrated_client.post(
                f"{API}/discover", json={}, headers=_auth(cam_auth_token)
            )
        assert resp.status_code == 200
        assert resp.json() == []

    def test_discover_with_range_calls_scan_range(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        with patch(
            "timelapse_manager.api.cameras.scan_range",
            new=AsyncMock(return_value=[]),
        ) as mock_scan:
            resp = migrated_client.post(
                f"{API}/discover",
                json={"range": "10.0.0.1-10.0.0.2"},
                headers=_auth(cam_auth_token),
            )
        assert resp.status_code == 200
        mock_scan.assert_called_once_with("10.0.0.1-10.0.0.2")

    def test_discover_over_cap_range_returns_422_and_does_not_scan(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        """A range beyond the 1024-host cap is refused (422) before scanning."""
        with patch(
            "timelapse_manager.api.cameras.scan_range",
            new=AsyncMock(return_value=[]),
        ) as mock_scan:
            resp = migrated_client.post(
                f"{API}/discover",
                json={"range": "10.0.0.0/16"},
                headers=_auth(cam_auth_token),
            )
        assert resp.status_code == 422, resp.text
        assert "over the limit of 1024" in resp.text
        mock_scan.assert_not_called()

    def test_discover_within_cap_range_proceeds_to_scan(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        """A range inside the cap passes the pre-check and reaches the scanner."""
        with patch(
            "timelapse_manager.api.cameras.scan_range",
            new=AsyncMock(return_value=[]),
        ) as mock_scan:
            resp = migrated_client.post(
                f"{API}/discover",
                json={"range": "192.168.1.0/24"},
                headers=_auth(cam_auth_token),
            )
        assert resp.status_code == 200, resp.text
        mock_scan.assert_called_once_with("192.168.1.0/24")

    def test_discover_enriches_resolved_uris_with_default(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        """A configured default credential resolves the discovered ONVIF URIs."""
        from timelapse_manager.cameras.base import DiscoveredCamera

        _enable_default_credentials(settings_no_autostart, "def-joe", "def-pw")
        found = [
            DiscoveredCamera(
                address="10.0.0.7",
                protocol="onvif",
                snapshot_uri=None,
                stream_uri=None,
                geolocation=None,
                vendor=None,
            )
        ]
        resolve = AsyncMock(
            return_value=("http://10.0.0.7/snap.jpg", "rtsp://10.0.0.7/s")
        )
        with (
            patch(
                "timelapse_manager.api.cameras.discover_onvif",
                new=AsyncMock(return_value=found),
            ),
            patch(
                "timelapse_manager.cameras.discovery.resolve_camera_host",
                side_effect=lambda a: a,
            ),
            patch(
                "timelapse_manager.cameras.discovery.OnvifAdapter.resolve_uris",
                resolve,
            ),
            patch(
                "timelapse_manager.cameras.discovery.OnvifAdapter.close",
                new=AsyncMock(),
            ),
        ):
            resp = migrated_client.post(
                f"{API}/discover", json={}, headers=_auth(cam_auth_token)
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body) == 1
        assert body[0]["snapshot_uri"] == "http://10.0.0.7/snap.jpg"
        assert body[0]["stream_uri"] == "rtsp://10.0.0.7/s"

    def test_discover_without_default_still_returns_results(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        """No default credential: discovery still returns, URIs stay None."""
        from timelapse_manager.cameras.base import DiscoveredCamera

        found = [
            DiscoveredCamera(
                address="10.0.0.8",
                protocol="onvif",
                snapshot_uri=None,
                stream_uri=None,
                geolocation=None,
                vendor=None,
            )
        ]
        with (
            patch(
                "timelapse_manager.api.cameras.discover_onvif",
                new=AsyncMock(return_value=found),
            ),
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
            resp = migrated_client.post(
                f"{API}/discover", json={}, headers=_auth(cam_auth_token)
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body[0]["address"] == "10.0.0.8"
        assert body[0]["snapshot_uri"] is None
        assert body[0]["stream_uri"] is None

    def test_discover_without_supervisor_does_not_error(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        """Absent capture engine: discovery skips enrichment, never 503/500."""
        from unittest.mock import MagicMock

        from timelapse_manager.cameras.base import DiscoveredCamera

        found = [
            DiscoveredCamera(
                address="10.0.0.9",
                protocol="onvif",
                snapshot_uri=None,
                stream_uri=None,
                geolocation=None,
                vendor=None,
            )
        ]
        no_supervisor = MagicMock()
        no_supervisor.capture_supervisor = None
        with (
            patch(
                "timelapse_manager.api.cameras.discover_onvif",
                new=AsyncMock(return_value=found),
            ),
            patch(
                "timelapse_manager.api.cameras.get_context",
                return_value=no_supervisor,
            ),
        ):
            resp = migrated_client.post(
                f"{API}/discover", json={}, headers=_auth(cam_auth_token)
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body[0]["address"] == "10.0.0.9"
        assert body[0]["snapshot_uri"] is None


# ---------------------------------------------------------------------------
# Protocol-detection endpoint
# ---------------------------------------------------------------------------

DETECT = f"{API}/detect-protocol"

# The cloud-metadata address: always denied by the SSRF guard, no DNS involved.
_DENIED_ADDRESS = "169.254.169.254"


def _enable_default_credentials(
    settings: Settings, username: str, password: str
) -> None:
    """Enable the global default camera credentials in the shared DB."""
    from timelapse_manager.security.camera_defaults_service import (
        CameraDefaultsUpdate,
        update_settings,
    )

    engine = create_db_engine(settings.database.url)
    factory = create_session_factory(engine)
    try:
        with session_scope(factory) as session:
            update_settings(
                session,
                CameraDefaultsUpdate(
                    enabled=True, username=username, password=password
                ),
            )
    finally:
        engine.dispose()


def _fake_outcome() -> object:
    """A DetectionOutcome with one ok VAPIX candidate, recommended."""
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
                snapshot_uri="http://10.0.0.5/axis-cgi/jpg/image.cgi",
                confidence=Confidence.HIGH,
                detail="ok",
            ),
            ProtocolCandidate(
                protocol="onvif", ok=False, confidence=Confidence.HIGH, detail="no"
            ),
        ],
        recommended_primary="vapix",
    )


class TestDetectProtocolEndpoint:
    def test_requires_auth(self, migrated_client: TestClient) -> None:
        resp = migrated_client.post(DETECT, json={"address": "10.0.0.5"})
        assert resp.status_code == 401

    def test_denied_address_returns_422_without_probing(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        with patch("timelapse_manager.api.cameras.detect_protocols") as mock_detect:
            resp = migrated_client.post(
                DETECT,
                json={"address": _DENIED_ADDRESS},
                headers=_auth(cam_auth_token),
            )
        assert resp.status_code == 422
        # Crucially, no probe ran for a denied address.
        mock_detect.assert_not_called()

    def test_viewer_session_cookie_is_forbidden(
        self, viewer_client: TestClient
    ) -> None:
        """A viewer authenticated via the web session is rejected with 403.

        The detect-protocol endpoint uses require_operator_or_admin_principal,
        which checks the session cookie path when a cookie is present. A viewer
        role in that cookie is rejected as 403 before the local bearer-token
        fallback runs. This test uses the viewer_client fixture (session-cookie
        path) to exercise the role gate on the JSON API surface.

        Note: viewer_client uses web_settings (https://testserver base_url) and
        carries no Authorization header, so the endpoint's role dependency fires
        via the cookie branch and returns 403 for the viewer role.
        """
        resp = viewer_client.post(
            DETECT,
            json={"address": "10.0.0.5"},
        )
        assert resp.status_code == 403

    def test_returns_all_candidates_and_recommended(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        with patch(
            "timelapse_manager.api.cameras.detect_protocols",
            AsyncMock(return_value=_fake_outcome()),
        ):
            resp = migrated_client.post(
                DETECT,
                json={"address": "10.0.0.5"},
                headers=_auth(cam_auth_token),
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["recommended_primary"] == "vapix"
        protocols = {c["protocol"] for c in body["candidates"]}
        assert protocols == {"vapix", "onvif"}

    def test_response_never_contains_credentials(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        with patch(
            "timelapse_manager.api.cameras.detect_protocols",
            AsyncMock(return_value=_fake_outcome()),
        ):
            resp = migrated_client.post(
                DETECT,
                json={
                    "address": "10.0.0.5",
                    "credentials": {"username": "joe", "password": "sup3r-secret"},
                },
                headers=_auth(cam_auth_token),
            )
        assert resp.status_code == 200
        assert "sup3r-secret" not in resp.text
        assert "joe" not in resp.text

    def test_blank_credentials_substitute_saved_default(
        self,
        migrated_client: TestClient,
        cam_auth_token: str,
        settings_no_autostart: Settings,
    ) -> None:
        """With no creds in the request, the saved default pair reaches the probe."""
        _enable_default_credentials(
            settings_no_autostart, "default-joe", "default-pass"
        )
        mock_detect = AsyncMock(return_value=_fake_outcome())
        with patch("timelapse_manager.api.cameras.detect_protocols", mock_detect):
            resp = migrated_client.post(
                DETECT,
                json={"address": "10.0.0.5"},
                headers=_auth(cam_auth_token),
            )
        assert resp.status_code == 200
        # detect_protocols(address, credentials, http_client, *, ...)
        passed_credentials = mock_detect.await_args.args[1]
        assert passed_credentials == ("default-joe", "default-pass")

    def test_supplied_credentials_reach_the_probe(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        mock_detect = AsyncMock(return_value=_fake_outcome())
        with patch("timelapse_manager.api.cameras.detect_protocols", mock_detect):
            resp = migrated_client.post(
                DETECT,
                json={
                    "address": "10.0.0.5",
                    "credentials": {"username": "joe", "password": "pw"},
                },
                headers=_auth(cam_auth_token),
            )
        assert resp.status_code == 200
        assert mock_detect.await_args.args[1] == ("joe", "pw")


# ---------------------------------------------------------------------------
# Capture status endpoint
# ---------------------------------------------------------------------------


class TestCaptureStatusEndpoint:
    def test_capture_status_for_known_camera_returns_200(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        created = _create_camera(migrated_client, cam_auth_token, name="status-cam")
        resp = migrated_client.get(
            f"{API}/{created['id']}/capture-status",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["camera_id"] == created["id"]
        assert isinstance(body["projects"], list)

    def test_capture_status_for_unknown_camera_returns_404(
        self, migrated_client: TestClient, cam_auth_token: str
    ) -> None:
        resp = migrated_client.get(
            f"{API}/99999/capture-status",
            headers=_auth(cam_auth_token),
        )
        assert resp.status_code == 404
