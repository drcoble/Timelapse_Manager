"""The thumbnail route holds no database connection while it waits or generates (#182).

The route used to keep the request's session (a pooled connection and a read
snapshot, which can stop a passive WAL checkpoint finishing) open for up to 2 s of
waiting for a generation slot plus the ffmpeg run. It now ends the transaction
once the frame is resolved. The tests count pool checkouts from inside the fake
ffmpeg and from the moment the slot is requested.

Refs #182.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, event
from sqlalchemy.orm import Session

from tests.web.test_frame_image import (
    _JPEG_BYTES,
    _seed_camera,
    _seed_project,
    _write_default_frame,
)
from tests.web.thumbnail_support import (
    FAKE_THUMB_BYTES,
    FakeFfmpeg,
    install_fake_ffmpeg,
)
from timelapse_manager.encode import thumbnail
from timelapse_manager.runtime import get_context


class _Checkouts:
    """Counts the pooled connections currently checked out of the engine."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.out = 0
        event.listen(engine, "checkout", self._checkout)
        event.listen(engine, "checkin", self._checkin)

    def _checkout(self, *_args: Any) -> None:
        self.out += 1

    def _checkin(self, *_args: Any) -> None:
        self.out -= 1

    def close(self) -> None:
        event.remove(self.engine, "checkout", self._checkout)
        event.remove(self.engine, "checkin", self._checkin)


@pytest.fixture()
def checkouts() -> Iterator[_Checkouts]:
    engine = get_context().session_factory.kw["bind"]
    assert isinstance(engine, Engine)
    counter = _Checkouts(engine)
    try:
        yield counter
    finally:
        counter.close()


class _WatchedFfmpeg(FakeFfmpeg):
    """A fake ffmpeg that records the checkouts outstanding when it runs."""

    def __init__(self, counter: _Checkouts) -> None:
        super().__init__()
        self.counter = counter
        self.outstanding_during_run: list[int] = []

    def run(self, args: list[str], *a: Any, **k: Any) -> Any:
        self.outstanding_during_run.append(self.counter.out)
        return super().run(args, *a, **k)


class TestThumbnailReleasesItsSession:
    def test_no_connection_is_held_while_ffmpeg_runs(
        self,
        admin_client: TestClient,
        checkouts: _Checkouts,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Rel Run", camera_id=cam)
        fid = _write_default_frame(pid)
        watched = _WatchedFfmpeg(checkouts)
        monkeypatch.setattr(thumbnail.subprocess, "run", watched.run)

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        assert resp.status_code == 200
        assert resp.content == FAKE_THUMB_BYTES
        assert watched.outstanding_during_run == [0]

    def test_no_connection_is_held_while_waiting_for_a_slot(
        self,
        admin_client: TestClient,
        checkouts: _Checkouts,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        cam = _seed_camera()
        pid = _seed_project(name="Rel Wait", camera_id=cam)
        fid = _write_default_frame(pid)
        install_fake_ffmpeg(monkeypatch)
        monkeypatch.setattr(thumbnail, "SLOT_TIMEOUT_SECONDS", 0.05)
        seen_when_waiting: list[int] = []
        real_slot = thumbnail.generation_slot

        def watched_slot(limit: int) -> Any:
            seen_when_waiting.append(checkouts.out)
            return real_slot(limit)

        monkeypatch.setattr(thumbnail, "generation_slot", watched_slot)
        limit = get_context().settings.render.thumbnail_concurrency

        with contextlib.ExitStack() as stack:
            for _ in range(limit):
                assert stack.enter_context(real_slot(limit))
            busy = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")

        assert busy.status_code == 200
        assert busy.content == _JPEG_BYTES  # the full frame, slot never free
        assert seen_when_waiting == [0]

    def test_the_request_session_is_committed_never_rolled_back(
        self,
        admin_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Releasing the session early keeps what the auth dependency wrote."""
        cam = _seed_camera()
        pid = _seed_project(name="Rel Commit", camera_id=cam)
        fid = _write_default_frame(pid)
        install_fake_ffmpeg(monkeypatch)
        commits: list[int] = []
        rollbacks: list[int] = []

        def on_commit(_session: Session) -> None:
            commits.append(1)

        def on_rollback(_session: Session) -> None:
            rollbacks.append(1)

        event.listen(Session, "after_commit", on_commit)
        event.listen(Session, "after_rollback", on_rollback)
        try:
            resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")
        finally:
            event.remove(Session, "after_commit", on_commit)
            event.remove(Session, "after_rollback", on_rollback)

        assert resp.status_code == 200
        assert commits
        assert rollbacks == []
