"""A non-ASCII bearer token is a 401, never a 500 (#115).

``secrets.compare_digest`` raises ``TypeError`` for a ``str`` holding
non-ASCII characters. Starlette decodes headers as latin-1, so a bearer token
with bytes 0x80-0xFF used to reach it as such a ``str``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.mark.parametrize("path", ["/api/v1/system/render-activity", "/api/v1/projects"])
@pytest.mark.parametrize("token", ["café", "ÿ" * 43])
def test_non_ascii_bearer_is_401(
    migrated_client: TestClient, path: str, token: str
) -> None:
    resp = migrated_client.get(
        path, headers={"Authorization": f"Bearer {token}".encode("latin-1")}
    )
    assert resp.status_code == 401, resp.text


def test_valid_token_still_works(
    migrated_client: TestClient, cam_auth_token: str
) -> None:
    resp = migrated_client.get(
        "/api/v1/projects", headers={"Authorization": f"Bearer {cam_auth_token}"}
    )
    assert resp.status_code == 200
