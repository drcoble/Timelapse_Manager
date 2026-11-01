"""A non-finite number in an API body is a 422, never a 500 (#97).

``json.loads`` accepts the ``NaN``, ``Infinity`` and ``-Infinity`` literals.
FastAPI's default validation handler echoes each bad input back in the 422
body, and Starlette's ``JSONResponse`` refuses to encode a non-finite float,
so the error response itself used to fail with a 500.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

LITERALS = ["NaN", "Infinity", "-Infinity"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# (path, JSON body template with {v} where the literal goes). Each body is
# otherwise valid enough that the non-finite field is what fails validation.
CASES = [
    # int fields
    (
        "/api/v1/projects",
        '{{"name": "nan", "camera_id": 1, "capture_interval_seconds": {v}}}',
    ),
    ("/api/v1/reports", '{{"title": "t", "description": "d", "project_id": {v}}}'),
    # a non-finite value inside a nested model, int and float fields
    ("/api/v1/projects/1/renders", '{{"output": {{"width": {v}}}}}'),
    ("/api/v1/projects/1/renders", '{{"output": {{"fps": {v}}}}}'),
]


@pytest.mark.parametrize("literal", LITERALS)
@pytest.mark.parametrize(("path", "template"), CASES)
def test_non_finite_input_is_422(
    migrated_client: TestClient,
    cam_auth_token: str,
    path: str,
    template: str,
    literal: str,
) -> None:
    resp = migrated_client.post(
        path, content=template.format(v=literal), headers=_auth(cam_auth_token)
    )
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert isinstance(body["detail"], list)
    # The echoed input is rendered as a string, not a bare NaN token.
    assert "NaN" not in resp.text.replace('"NaN"', "")
    assert "Infinity" not in resp.text.replace('"Infinity"', "").replace(
        '"-Infinity"', ""
    )


def test_finite_validation_error_keeps_fastapi_shape(
    migrated_client: TestClient, cam_auth_token: str
) -> None:
    resp = migrated_client.post(
        "/api/v1/projects",
        content='{"name": "x", "camera_id": 1, "capture_interval_seconds": 0}',
        headers=_auth(cam_auth_token),
    )
    assert resp.status_code == 422
    (err,) = [
        e for e in resp.json()["detail"] if e["loc"][-1] == "capture_interval_seconds"
    ]
    assert err["input"] == 0
    assert err["type"] == "greater_than_equal"


@pytest.mark.parametrize("fps", ["0", "-1", "240.5"])
def test_render_fps_out_of_range_is_422(
    migrated_client: TestClient, cam_auth_token: str, fps: str
) -> None:
    resp = migrated_client.post(
        "/api/v1/projects/1/renders",
        content=f'{{"output": {{"fps": {fps}}}}}',
        headers=_auth(cam_auth_token),
    )
    assert resp.status_code == 422, resp.text
    assert any(e["loc"][-1] == "fps" for e in resp.json()["detail"])


@pytest.mark.parametrize("literal", LITERALS)
def test_camera_coordinates_non_finite_is_422(
    migrated_client: TestClient, cam_auth_token: str, literal: str
) -> None:
    """The camera route checks coordinates itself (#85) and still answers 422."""
    resp = migrated_client.post(
        "/api/v1/cameras",
        content=(
            '{"name": "c", "address": "127.0.0.1", '
            f'"geolocation_latitude": {literal}}}'
        ),
        headers=_auth(cam_auth_token),
    )
    assert resp.status_code == 422, resp.text
