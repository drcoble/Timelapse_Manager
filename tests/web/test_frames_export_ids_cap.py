"""``POST /frames/export`` with very long explicit id lists (#96).

An explicit ``frame_ids`` list longer than SQLite's 32,766 bound-variable limit
used to build one unchunked ``IN (...)`` and return a 500. The project lookup is
now chunked, and the list obeys the same ceiling as a materialised range
(``frames.max_range_ids``): over it is a 413.

Refs #96.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient
from sqlalchemy import insert

from tests.conftest import csrf_of
from tests.web.test_frames_export_routes import (
    _FORM,
    _latest_export_job,
    _seed_project,
)
from timelapse_manager.db.models import Frame
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context

_BASE = datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None)


def _seed_many_frames(project_id: int, count: int) -> list[int]:
    """Insert ``count`` frames in one statement; return their ids in order."""
    ctx = get_context()
    rows = [
        {
            "project_id": project_id,
            "sequence_index": seq,
            "capture_timestamp": _BASE,
            "file_path": f"{seq:08d}.jpg",
            "width": 640,
            "height": 480,
            "file_size_bytes": 1000,
            "capture_status": "captured",
            "origin": "captured",
            "lifecycle_state": "active",
        }
        for seq in range(1, count + 1)
    ]
    with session_scope(ctx.session_factory) as db:
        db.execute(insert(Frame), rows)
        return [
            row[0]
            for row in db.query(Frame.id)
            .filter(Frame.project_id == project_id)
            .order_by(Frame.sequence_index)
            .all()
        ]


class TestExportIdListSize:
    def test_forty_thousand_ids_are_accepted(self, operator_client: TestClient) -> None:
        pid = _seed_project(name="Exp 40k")
        ids = _seed_many_frames(pid, 40_000)
        csrf = csrf_of(operator_client, "/frames")

        resp = operator_client.post(
            "/frames/export",
            data={"frame_ids": ",".join(map(str, ids)), "csrf_token": csrf},
            headers=_FORM,
        )

        assert resp.status_code == 202, resp.text
        job = _latest_export_job(pid)
        assert job.id == resp.json()["job_id"]
        assert len((job.output_settings or {})["frame_ids"]) == 40_000

    def test_ids_over_the_cap_are_a_413_and_queue_nothing(
        self, operator_client: TestClient
    ) -> None:
        pid = _seed_project(name="Exp over cap")
        limit = get_context().settings.frames.max_range_ids
        ids = ",".join(str(n) for n in range(1, limit + 2))
        csrf = csrf_of(operator_client, "/frames")

        resp = operator_client.post(
            "/frames/export",
            data={"frame_ids": ids, "csrf_token": csrf},
            headers=_FORM,
        )

        assert resp.status_code == 413, resp.text
        assert "error" in resp.json()
        ctx = get_context()
        with session_scope(ctx.session_factory) as db:
            from timelapse_manager.db.models import RenderJob

            assert db.query(RenderJob).filter(RenderJob.project_id == pid).count() == 0

    def test_a_long_list_spanning_two_projects_is_still_refused(
        self, operator_client: TestClient
    ) -> None:
        first = _seed_project(name="Exp span A")
        second = _seed_project(name="Exp span B")
        ids_a = _seed_many_frames(first, 36_000)
        ids_b = _seed_many_frames(second, 10)
        csrf = csrf_of(operator_client, "/frames")

        resp = operator_client.post(
            "/frames/export",
            data={
                "frame_ids": ",".join(map(str, ids_a + ids_b)),
                "csrf_token": csrf,
            },
            headers=_FORM,
        )

        assert resp.status_code == 400, resp.text
        assert "single project" in resp.json()["error"]
