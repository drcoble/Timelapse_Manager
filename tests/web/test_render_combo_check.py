"""The live encoder/container combination-check fragment endpoint.

``GET /renders/combo-check`` returns a ``.alert warning`` fragment for an
unsupported pair (so HTMX shows it) and an empty body for a valid pair (so HTMX
clears any prior warning). It is always HTTP 200 for an authorised operator, and
role-gated below that.
"""

from __future__ import annotations

from fastapi.testclient import TestClient


class TestRenderComboCheck:
    def test_invalid_combination_returns_warning(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.get(
            "/renders/combo-check",
            params={"render_encoder": "libvpx-vp9", "render_container": "mp4"},
        )
        assert resp.status_code == 200
        assert 'class="alert warning"' in resp.text
        assert "cannot be stored" in resp.text

    def test_valid_combination_returns_empty(self, admin_client: TestClient) -> None:
        resp = admin_client.get(
            "/renders/combo-check",
            params={"render_encoder": "libx264", "render_container": "mp4"},
        )
        assert resp.status_code == 200
        assert "alert" not in resp.text
        assert resp.text.strip() == ""

    def test_webm_with_h264_warns(self, admin_client: TestClient) -> None:
        resp = admin_client.get(
            "/renders/combo-check",
            params={"render_encoder": "libx264", "render_container": "webm"},
        )
        assert resp.status_code == 200
        assert 'class="alert warning"' in resp.text

    def test_mkv_with_vp9_is_valid(self, admin_client: TestClient) -> None:
        resp = admin_client.get(
            "/renders/combo-check",
            params={"render_encoder": "libvpx-vp9", "render_container": "mkv"},
        )
        assert resp.status_code == 200
        # VP9+MKV is a valid (muxable) combination, so there is no incompatibility
        # warning. It is not browser-streamable, however, so the download-only
        # info notice is expected.
        assert 'class="alert warning"' not in resp.text
        assert "Download only" in resp.text

    def test_requires_authentication(self, web_client: TestClient) -> None:
        # Anonymous request is redirected to login (operator gate), never 200.
        resp = web_client.get(
            "/renders/combo-check",
            params={"render_encoder": "libx264", "render_container": "mp4"},
            follow_redirects=False,
        )
        assert resp.status_code != 200
