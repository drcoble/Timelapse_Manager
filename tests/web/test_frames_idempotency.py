"""``Idempotency-Key`` on the frame mutations the browser drives from JS (#70 F-R2).

A double click or a retried request must not shift the frames twice or enqueue a
second export. The same key replays the first response; a different user's key
is independent; a request without the header behaves as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from tests.conftest import LOOPBACK_PEER, csrf_of, login
from timelapse_manager.db.models import Camera, Frame, Project, RenderJob
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.web.idempotency import REPLAYED_HEADER

_FORM = {"Content-Type": "application/x-www-form-urlencoded"}
_BASE_TS = datetime(2026, 1, 1, 12, 0, tzinfo=UTC).replace(tzinfo=None)


def _seed_project_with_frames(name: str, count: int = 2) -> tuple[int, list[int]]:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="127.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            capture_interval_seconds=60,
            lifecycle_state="active",
        )
        db.add(proj)
        db.flush()
        ids = []
        for seq in range(count):
            frame = Frame(
                project_id=proj.id,
                sequence_index=seq,
                capture_timestamp=_BASE_TS + timedelta(minutes=seq),
                file_path=f"{seq:08d}.jpg",
                capture_status="captured",
                origin="captured",
                lifecycle_state="active",
            )
            db.add(frame)
            db.flush()
            ids.append(frame.id)
        return proj.id, ids


def _capture_ts(frame_id: int) -> datetime | None:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        frame = db.get(Frame, frame_id)
        assert frame is not None
        return frame.capture_timestamp


def _export_job_count(project_id: int) -> int:
    ctx = get_context()
    with session_scope(ctx.session_factory) as db:
        return (
            db.query(RenderJob)
            .filter(RenderJob.project_id == project_id, RenderJob.kind == "export")
            .count()
        )


def _post(
    client: TestClient, path: str, data: dict[str, str], key: str | None
) -> httpx.Response:
    headers = {**_FORM, "X-CSRF-Token": csrf_of(client, "/frames")}
    if key is not None:
        headers["Idempotency-Key"] = key
    return client.post(path, data=data, headers=headers)


def _second_admin_client(client: TestClient) -> TestClient:
    """A separate cookie jar on the same app, signed in as the seeded admin."""
    other = TestClient(client.app, base_url="https://testserver", client=LOOPBACK_PEER)
    login(other)
    return other


class TestOffsetIdempotency:
    def test_same_key_shifts_once_and_replays_body(
        self, operator_client: TestClient
    ) -> None:
        _, ids = _seed_project_with_frames("Idem Offset")
        before = _capture_ts(ids[0])
        assert before is not None
        data = {"frame_ids": ",".join(map(str, ids)), "seconds": "3600"}
        key = "0f8fad5b-d9cb-469f-a165-70867728950e"

        first = _post(operator_client, "/frames/offset", data, key)
        second = _post(operator_client, "/frames/offset", data, key)

        assert first.status_code == 200
        assert second.status_code == 200
        assert second.content == first.content
        assert second.headers.get(REPLAYED_HEADER) == "true"
        assert _capture_ts(ids[0]) == before + timedelta(seconds=3600)

    def test_new_key_is_a_new_action(self, operator_client: TestClient) -> None:
        _, ids = _seed_project_with_frames("Idem Offset New Key")
        before = _capture_ts(ids[0])
        assert before is not None
        data = {"frame_ids": str(ids[0]), "seconds": "60"}
        _post(operator_client, "/frames/offset", data, "key-aaaaaaaa")
        _post(operator_client, "/frames/offset", data, "key-bbbbbbbb")
        assert _capture_ts(ids[0]) == before + timedelta(seconds=120)

    def test_without_header_is_unchanged(self, operator_client: TestClient) -> None:
        _, ids = _seed_project_with_frames("Idem Offset No Header")
        before = _capture_ts(ids[0])
        assert before is not None
        data = {"frame_ids": str(ids[0]), "seconds": "60"}
        _post(operator_client, "/frames/offset", data, None)
        _post(operator_client, "/frames/offset", data, None)
        assert _capture_ts(ids[0]) == before + timedelta(seconds=120)

    def test_failed_request_can_be_retried_with_same_key(
        self, operator_client: TestClient
    ) -> None:
        _, ids = _seed_project_with_frames("Idem Offset Retry")
        before = _capture_ts(ids[0])
        assert before is not None
        key = "retry-key-0001"
        bad = _post(
            operator_client,
            "/frames/offset",
            {"frame_ids": str(ids[0]), "seconds": "abc"},
            key,
        )
        assert bad.status_code == 400
        # A failed request is not stored, so the key is free; the corrected body
        # is a different request, which the fingerprint must still allow because
        # nothing was recorded for this key.
        good = _post(
            operator_client,
            "/frames/offset",
            {"frame_ids": str(ids[0]), "seconds": "60"},
            key,
        )
        assert good.status_code == 200
        assert _capture_ts(ids[0]) == before + timedelta(seconds=60)

    def test_reused_key_with_different_body_is_rejected(
        self, operator_client: TestClient
    ) -> None:
        _, ids = _seed_project_with_frames("Idem Offset Mismatch")
        before = _capture_ts(ids[0])
        assert before is not None
        key = "mismatch-key-01"
        _post(
            operator_client,
            "/frames/offset",
            {"frame_ids": str(ids[0]), "seconds": "60"},
            key,
        )
        resp = _post(
            operator_client,
            "/frames/offset",
            {"frame_ids": str(ids[0]), "seconds": "-60"},
            key,
        )
        assert resp.status_code == 422
        assert _capture_ts(ids[0]) == before + timedelta(seconds=60)

    def test_malformed_key_rejected_without_change(
        self, operator_client: TestClient
    ) -> None:
        _, ids = _seed_project_with_frames("Idem Offset Bad Key")
        before = _capture_ts(ids[0])
        resp = _post(
            operator_client,
            "/frames/offset",
            {"frame_ids": str(ids[0]), "seconds": "60"},
            "bad key!",
        )
        assert resp.status_code == 400
        assert _capture_ts(ids[0]) == before

    def test_other_user_same_key_is_independent(
        self, operator_client: TestClient
    ) -> None:
        _, ids = _seed_project_with_frames("Idem Offset Users")
        before = _capture_ts(ids[0])
        assert before is not None
        data = {"frame_ids": str(ids[0]), "seconds": "60"}
        key = "shared-key-0001"
        _post(operator_client, "/frames/offset", data, key)
        admin = _second_admin_client(operator_client)
        resp = _post(admin, "/frames/offset", data, key)
        assert resp.status_code == 200
        assert REPLAYED_HEADER.lower() not in resp.headers
        assert _capture_ts(ids[0]) == before + timedelta(seconds=120)


class TestExportIdempotency:
    def test_same_key_enqueues_one_job(self, operator_client: TestClient) -> None:
        pid, ids = _seed_project_with_frames("Idem Export")
        data = {"frame_ids": ",".join(map(str, ids))}
        key = "7c9e6679-7425-40de-944b-e07fc1f90ae7"

        first = _post(operator_client, "/frames/export", data, key)
        second = _post(operator_client, "/frames/export", data, key)

        assert first.status_code == 202
        assert second.status_code == 202
        assert second.json()["job_id"] == first.json()["job_id"]
        assert _export_job_count(pid) == 1

    def test_without_header_enqueues_each_time(
        self, operator_client: TestClient
    ) -> None:
        pid, ids = _seed_project_with_frames("Idem Export No Header")
        data = {"frame_ids": ",".join(map(str, ids))}
        _post(operator_client, "/frames/export", data, None)
        _post(operator_client, "/frames/export", data, None)
        assert _export_job_count(pid) == 2

    def test_other_user_same_key_is_independent(
        self, operator_client: TestClient
    ) -> None:
        pid, ids = _seed_project_with_frames("Idem Export Users")
        data = {"frame_ids": ",".join(map(str, ids))}
        key = "shared-export-key"
        first = _post(operator_client, "/frames/export", data, key)
        admin = _second_admin_client(operator_client)
        second = _post(admin, "/frames/export", data, key)
        assert second.status_code == 202
        assert second.json()["job_id"] != first.json()["job_id"]
        assert _export_job_count(pid) == 2
