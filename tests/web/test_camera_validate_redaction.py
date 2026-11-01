"""The camera connection-validation fragment must not disclose or inject.

``POST /cameras/{id}/validate`` echoes the adapter's diagnostic message straight
into an HTML fragment. That message can contain the probed camera address, and an
address may embed ``user:pass@host`` userinfo -- so the echo path must both strip
the embedded URL credential (no credential disclosure) and HTML-escape the value
(no markup injection) before it reaches the operator's page.

These drive the real admin-gated, CSRF-protected route through the running app
with a mocked adapter, so they pin the response the operator actually receives.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from tests.conftest import csrf_of
from timelapse_manager.cameras.base import ValidationFailure, ValidationResult
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_ALLOWED_ADDRESS = "192.168.1.50"


def _seed_camera(*, name: str) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=name, address=_ALLOWED_ADDRESS, protocol="vapix")
        db.add(cam)
        db.flush()
        return cam.id


def _validate(client: TestClient, camera_id: int, message: str) -> str:
    """POST validate with a mocked adapter whose probe message is ``message``."""
    csrf = csrf_of(client, "/cameras")
    fake_adapter = AsyncMock()
    fake_adapter.validate_connection = AsyncMock(
        return_value=ValidationResult(
            ok=False, reason=ValidationFailure.UNREACHABLE, message=message
        )
    )
    fake_adapter.close = AsyncMock()
    with patch("timelapse_manager.cameras.build_adapter", return_value=fake_adapter):
        resp = client.post(
            f"/cameras/{camera_id}/validate",
            data={"csrf_token": csrf},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    assert resp.status_code == 200, resp.text
    return resp.text


class TestValidateFragmentRedaction:
    def test_credentialed_url_in_message_is_redacted(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="cred-echo-cam")
        text = _validate(
            admin_client,
            camera_id,
            "could not reach rtsp://admin:s3cr3t-pw@192.168.1.50/stream",
        )
        # The embedded credential must be stripped, not echoed verbatim.
        assert "s3cr3t-pw" not in text
        assert "admin:s3cr3t-pw" not in text
        # The rest of the URL is kept, with the userinfo masked.
        assert "***@192.168.1.50" in text

    def test_markup_in_message_is_html_escaped(self, admin_client: TestClient) -> None:
        camera_id = _seed_camera(name="xss-echo-cam")
        text = _validate(
            admin_client,
            camera_id,
            "bad host <script>alert(1)</script>",
        )
        # The angle brackets must be escaped so the payload cannot execute; only
        # the fragment's own <span> wrapper may contain raw markup.
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;" in text

    def test_credential_and_markup_together_are_both_neutralised(
        self, admin_client: TestClient
    ) -> None:
        camera_id = _seed_camera(name="combo-echo-cam")
        text = _validate(
            admin_client,
            camera_id,
            "rtsp://u:p@192.168.1.50/<img src=x onerror=alert(1)>",
        )
        assert "u:p@" not in text
        assert "***@192.168.1.50" in text
        assert "<img src=x onerror=alert(1)>" not in text
        assert "&lt;img" in text
