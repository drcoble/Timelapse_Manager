"""The streamed frame import and upload, end to end (#67 P1, WP-67-N).

Covers the CSRF check before any upload byte is written, the live access
re-check after the body, the caps enforced while streaming, spool cleanup on
every exit, and the work running on the jobs executor.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import struct
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.concurrency import run_in_threadpool

import timelapse_manager.web.routers.frames as web_frames
from tests.conftest import LOOPBACK_PEER, csrf_of
from timelapse_manager.db.models import Camera, Frame, Project, User
from timelapse_manager.db.session import session_scope
from timelapse_manager.runtime import get_context
from timelapse_manager.security.sessions import revoke_all_user_sessions
from timelapse_manager.security.token import ensure_local_token
from timelapse_manager.services import frames as frame_service
from timelapse_manager.web import body_limit
from timelapse_manager.web.multipart import FRAME_IMPORT_PATH, spool_root

_B = b"streamboundary1"
_CT = f"multipart/form-data; boundary={_B.decode()}"


def _jpeg(width: int = 320, height: int = 240) -> bytes:
    sof = (
        b"\xff\xc0"
        + struct.pack(">H", 17)
        + b"\x08"
        + struct.pack(">H", height)
        + struct.pack(">H", width)
        + b"\x01\x01\x11\x00"
    )
    return b"\xff\xd8" + sof + b"\xff\xd9"


def _part(name: str, content: bytes, filename: str | None = None) -> bytes:
    disposition = f'form-data; name="{name}"'
    if filename is not None:
        disposition += f'; filename="{filename}"'
    return (
        b"--"
        + _B
        + b"\r\nContent-Disposition: "
        + disposition.encode()
        + b"\r\n\r\n"
        + content
        + b"\r\n"
    )


def _form(*parts: bytes) -> bytes:
    return b"".join(parts) + b"--" + _B + b"--\r\n"


def _seed_project(name: str) -> int:
    ctx = get_context()
    frames_dir = Path(tempfile.mkdtemp(prefix=f"stream-{name}-"))
    with session_scope(ctx.session_factory) as db:
        cam = Camera(name=f"{name}-cam", address="10.0.0.1", protocol="vapix")
        db.add(cam)
        db.flush()
        proj = Project(
            camera_id=cam.id,
            name=name,
            lifecycle_state="active",
            operational_status="idle",
            storage_path=str(frames_dir),
        )
        db.add(proj)
        db.flush()
        return proj.id


def _frame_count(project_id: int) -> int:
    with session_scope(get_context().session_factory) as db:
        return db.query(Frame).filter(Frame.project_id == project_id).count()


def _spool_entries() -> list[Path]:
    root = spool_root(Path(get_context().settings.paths.data_dir))
    return sorted(root.iterdir()) if root.exists() else []


def _post(client: TestClient, project_id: int, body: bytes, **headers: str) -> Any:
    return client.post(
        f"/projects/{project_id}/frames/import",
        content=body,
        headers={"Content-Type": _CT, **headers},
    )


class _SpoolSpy:
    """Counts how many times the route began streaming a body to disk."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = web_frames.stream_multipart

        async def spy(*args: Any, **kwargs: Any) -> Any:
            self.calls += 1
            return await real(*args, **kwargs)

        monkeypatch.setattr(web_frames, "stream_multipart", spy)


class TestNoJsCsrfBeforeAnyUploadByte:
    def test_leading_csrf_part_is_accepted(self, operator_client: TestClient) -> None:
        project_id = _seed_project("csrf-ok")
        token = csrf_of(operator_client, "/")
        body = _form(
            _part("csrf_token", token.encode()), _part("files", _jpeg(), "a.jpg")
        )
        resp = _post(operator_client, project_id, body)
        assert resp.status_code == 200
        assert _frame_count(project_id) == 1
        assert _spool_entries() == []

    def test_header_token_still_works(self, operator_client: TestClient) -> None:
        project_id = _seed_project("csrf-header")
        token = csrf_of(operator_client, "/")
        body = _form(_part("files", _jpeg(), "a.jpg"))
        resp = _post(operator_client, project_id, body, **{"X-CSRF-Token": token})
        assert resp.status_code == 200
        assert _frame_count(project_id) == 1

    @pytest.mark.parametrize(
        "make_body",
        [
            pytest.param(
                lambda t: _form(_part("files", _jpeg(), "a.jpg")), id="missing"
            ),
            pytest.param(
                lambda t: _form(
                    _part("csrf_token", b"wrong"), _part("files", _jpeg(), "a.jpg")
                ),
                id="wrong",
            ),
            pytest.param(
                lambda t: _form(
                    _part("files", _jpeg(), "a.jpg"), _part("csrf_token", t)
                ),
                id="after-a-file-part",
            ),
            pytest.param(
                lambda t: _form(
                    _part("note", b"x"),
                    _part("csrf_token", t),
                    _part("files", _jpeg(), "a.jpg"),
                ),
                id="not-first",
            ),
            pytest.param(
                lambda t: _form(
                    _part("csrf_token", t, "token.txt"),
                    _part("files", _jpeg(), "a.jpg"),
                ),
                id="sent-as-a-file",
            ),
            pytest.param(
                lambda t: _form(
                    _part("csrf_token", b"wrong"),
                    _part("csrf_token", t),
                    _part("files", _jpeg(), "a.jpg"),
                ),
                id="valid-duplicate-after-wrong",
            ),
            pytest.param(lambda t: b"--" + _B + b"\r\ngarbage", id="truncated"),
        ],
    )
    def test_refused_before_anything_is_spooled(
        self,
        operator_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        make_body: Callable[[bytes], bytes],
    ) -> None:
        project_id = _seed_project("csrf-bad")
        token = csrf_of(operator_client, "/").encode()
        spy = _SpoolSpy(monkeypatch)
        resp = _post(operator_client, project_id, make_body(token))
        assert resp.status_code == 403
        assert resp.text == "CSRF validation failed."
        assert spy.calls == 0  # the route never began reading the body
        assert _spool_entries() == []
        assert _frame_count(project_id) == 0

    def test_first_wins_so_the_two_readers_agree(
        self, operator_client: TestClient
    ) -> None:
        """A later wrong ``csrf_token`` cannot change an earlier valid one."""
        project_id = _seed_project("csrf-first-wins")
        token = csrf_of(operator_client, "/").encode()
        body = _form(
            _part("csrf_token", token),
            _part("csrf_token", b"wrong"),
            _part("files", _jpeg(), "a.jpg"),
        )
        assert _post(operator_client, project_id, body).status_code == 200
        assert _frame_count(project_id) == 1

    def test_the_template_puts_the_token_before_the_file_input(self) -> None:
        template = (
            Path(web_frames.__file__).parents[1]
            / "templates"
            / "_partials"
            / "frames_import_form.html"
        ).read_text()
        assert template.index('name="csrf_token"') < template.index('name="files"')

    def test_the_path_pattern_matches_the_body_limit_table(self) -> None:
        paths = [
            "/projects/1/frames/import",
            "/api/v1/projects/1/frames/import",
            "/projects/1/frames/import/",
            "/projects/1/frames/importx",
            "//projects/1/frames/import",
            "/api/v1/api/v1/projects/1/frames/import",
            "/projects//frames/import",
            "/projects/1/2/frames/import",
            "/PROJECTS/1/frames/import",
        ]
        for path in paths:
            assert bool(FRAME_IMPORT_PATH.fullmatch(path)) == bool(
                body_limit._FRAME_IMPORT_PATH.fullmatch(path)
            ), path


def _land_after_body(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[Any, User], None]
) -> None:
    """Make a change land after the body is read, before the route acts on it."""
    real = web_frames.stream_multipart

    async def slow(*args: Any, **kwargs: Any) -> Any:
        form = await real(*args, **kwargs)

        def mutate() -> None:
            with session_scope(get_context().session_factory) as db:
                user = db.query(User).filter(User.username == "operator").one()
                change(db, user)

        await run_in_threadpool(mutate)
        return form

    monkeypatch.setattr(web_frames, "stream_multipart", slow)


def _revoke(db: Any, user: User) -> None:
    revoke_all_user_sessions(db, user.id)


def _disable(db: Any, user: User) -> None:
    user.enabled = False


def _demote(db: Any, user: User) -> None:
    user.role = "viewer"


class TestLiveRecheckAfterTheBody:
    @pytest.mark.parametrize(
        ("change", "expected"),
        [
            pytest.param(_revoke, 401, id="revoked"),
            pytest.param(_disable, 401, id="disabled"),
            pytest.param(_demote, 403, id="demoted"),
        ],
    )
    def test_access_lost_while_uploading_stops_the_import(
        self,
        operator_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        change: Callable[[Any, User], None],
        expected: int,
    ) -> None:
        project_id = _seed_project("recheck")
        token = csrf_of(operator_client, "/")
        _land_after_body(monkeypatch, change)
        body = _form(_part("files", _jpeg(), "a.jpg"))
        resp = _post(operator_client, project_id, body, **{"X-CSRF-Token": token})
        assert resp.status_code == expected
        assert _frame_count(project_id) == 0
        assert _spool_entries() == []

    def test_without_a_change_the_import_succeeds(
        self, operator_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project_id = _seed_project("recheck-control")
        token = csrf_of(operator_client, "/")
        _land_after_body(monkeypatch, lambda db, user: None)
        resp = _post(
            operator_client,
            project_id,
            _form(_part("files", _jpeg(), "a.jpg")),
            **{"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert _frame_count(project_id) == 1


class TestLimitsAndCleanup:
    def _api(
        self, client: TestClient, project_id: int, body: bytes, **extra: str
    ) -> Any:
        token = ensure_local_token(get_context().settings)
        return client.post(
            f"/api/v1/projects/{project_id}/frames/import",
            content=body,
            headers={
                "Content-Type": _CT,
                "Authorization": f"Bearer {token}",
                **extra,
            },
        )

    def test_a_file_over_the_cap_is_413_and_leaves_nothing(
        self, web_client: TestClient
    ) -> None:
        get_context().settings.capture.max_image_bytes = 64 * 1024
        project_id = _seed_project("cap-file")
        body = _form(
            _part("files", _jpeg(), "ok.jpg"), _part("files", b"x" * 200_000, "big.jpg")
        )
        resp = self._api(web_client, project_id, body)
        assert resp.status_code == 413
        assert _frame_count(project_id) == 0
        assert _spool_entries() == []

    def test_more_files_than_the_batch_cap_is_422(self, web_client: TestClient) -> None:
        project_id = _seed_project("cap-count")
        parts = [_part("files", _jpeg(), f"{i}.jpg") for i in range(201)]
        resp = self._api(web_client, project_id, _form(*parts))
        assert resp.status_code == 422
        assert _frame_count(project_id) == 0
        assert _spool_entries() == []

    def test_web_cap_errors_are_inline_fragments(
        self, operator_client: TestClient
    ) -> None:
        get_context().settings.capture.max_image_bytes = 64 * 1024
        project_id = _seed_project("cap-web")
        token = csrf_of(operator_client, "/")
        resp = _post(
            operator_client,
            project_id,
            _form(_part("files", b"x" * 200_000, "big.jpg")),
            **{"X-CSRF-Token": token},
        )
        assert resp.status_code == 200
        assert "too large" in resp.text
        assert _spool_entries() == []

    @pytest.mark.parametrize(
        "make_body",
        [
            pytest.param(
                lambda: _form(
                    _part("junk", b"x" * 200_000), _part("files", _jpeg(), "a.jpg")
                ),
                id="huge-text-field",
            ),
            pytest.param(
                lambda: _form(
                    *[_part(f"f{i}", b"x") for i in range(100)],
                    _part("files", _jpeg(), "a.jpg"),
                ),
                id="many-small-fields",
            ),
            pytest.param(
                lambda: _form(
                    *[_part("x", b"y").replace(b'; name="x"', b"") for _ in range(100)],
                    _part("files", _jpeg(), "a.jpg"),
                ),
                id="many-unnamed-parts",
            ),
        ],
    )
    def test_text_part_abuse_is_400_and_leaves_nothing(
        self, web_client: TestClient, make_body: Callable[[], bytes]
    ) -> None:
        project_id = _seed_project("text-abuse")
        resp = self._api(web_client, project_id, make_body())
        assert resp.status_code == 400
        assert _frame_count(project_id) == 0
        assert _spool_entries() == []

    def test_low_disk_is_507_on_the_api_and_inline_on_the_web(
        self, operator_client: TestClient
    ) -> None:
        get_context().settings.storage.low_watermark_bytes = 1 << 62
        project_id = _seed_project("low-disk")
        token = csrf_of(operator_client, "/")
        body = _form(_part("files", _jpeg(), "a.jpg"))
        web = _post(operator_client, project_id, body, **{"X-CSRF-Token": token})
        assert web.status_code == 200
        assert "not enough free disk" in web.text
        api = self._api(operator_client, project_id, body, **{"X-CSRF-Token": token})
        assert api.status_code == 507
        assert _frame_count(project_id) == 0

    def test_a_hostile_filename_is_only_a_label(self, web_client: TestClient) -> None:
        project_id = _seed_project("traversal")
        evil = "../../../../tmp/evil.jpg"
        resp = self._api(web_client, project_id, _form(_part("files", _jpeg(), evil)))
        assert resp.status_code == 200
        assert resp.json()["imported_count"] == 1
        with session_scope(get_context().session_factory) as db:
            frame = db.query(Frame).filter(Frame.project_id == project_id).one()
            assert ".." not in frame.file_path
        assert not Path("/tmp/evil.jpg").exists()

    def test_the_import_runs_on_the_jobs_executor(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        threads: list[str] = []
        real = frame_service.import_frames

        def spy(*args: Any, **kwargs: Any) -> Any:
            threads.append(threading.current_thread().name)
            return real(*args, **kwargs)

        monkeypatch.setattr(frame_service, "import_frames", spy)
        project_id = _seed_project("jobs-pool")
        resp = self._api(
            web_client, project_id, _form(_part("files", _jpeg(), "a.jpg"))
        )
        assert resp.status_code == 200
        assert len(threads) == 1 and threads[0].startswith("tlm-jobs")
        assert _spool_entries() == []

    def test_raw_upload_over_the_image_cap_is_413_and_leaves_nothing(
        self, web_client: TestClient
    ) -> None:
        get_context().settings.capture.max_image_bytes = 64 * 1024
        project_id = _seed_project("raw-cap")
        token = ensure_local_token(get_context().settings)
        resp = web_client.post(
            f"/api/v1/projects/{project_id}/frames/upload",
            params={"capture_timestamp": "2026-10-27T12:00:00"},
            content=_jpeg() + b"\x00" * (64 * 1024),
            headers={
                "Content-Type": "application/octet-stream",
                "Authorization": f"Bearer {token}",
            },
        )
        assert resp.status_code == 413
        assert _frame_count(project_id) == 0
        assert _spool_entries() == []

    def test_raw_upload_succeeds_and_cleans_up(self, web_client: TestClient) -> None:
        project_id = _seed_project("raw-ok")
        token = ensure_local_token(get_context().settings)
        resp = web_client.post(
            f"/api/v1/projects/{project_id}/frames/upload",
            params={"capture_timestamp": "2026-10-27T12:00:00"},
            content=_jpeg(),
            headers={
                "Content-Type": "application/octet-stream",
                "Authorization": f"Bearer {token}",
            },
        )
        assert resp.status_code == 200, resp.text
        assert _frame_count(project_id) == 1
        assert _spool_entries() == []


def _drive(client: TestClient, path: str, chunks: list[bytes], token: str) -> None:
    """POST ``chunks`` to the app, then disconnect before the body is complete."""

    async def run() -> None:
        queue = list(chunks)

        async def receive() -> dict[str, Any]:
            if queue:
                return {
                    "type": "http.request",
                    "body": queue.pop(0),
                    "more_body": True,
                }
            await asyncio.sleep(0.05)
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            return None

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "server": ("testserver", 443),
            "client": LOOPBACK_PEER,
            "headers": [
                (b"host", b"testserver"),
                (b"content-type", _CT.encode()),
                (b"authorization", f"Bearer {token}".encode()),
            ],
        }
        await client.app(scope, receive, send)

    # The disconnect surfaces as an error out of the app.
    with contextlib.suppress(Exception):
        client.portal.call(run)  # type: ignore[union-attr]


def test_client_disconnect_mid_upload_leaves_no_spool(web_client: TestClient) -> None:
    project_id = _seed_project("disconnect")
    token = ensure_local_token(get_context().settings)
    first = _form(_part("files", _jpeg(), "a.jpg"))[:-20] + b"x" * 5000
    first = (
        b"--"
        + _B
        + b'\r\nContent-Disposition: form-data; name="files"; filename="a.jpg"\r\n\r\n'
        + _jpeg()
        + b"x" * 5000
    )
    _drive(web_client, f"/api/v1/projects/{project_id}/frames/import", [first], token)
    assert spool_root(Path(get_context().settings.paths.data_dir)).exists()  # it ran
    assert _frame_count(project_id) == 0
    assert _spool_entries() == []


def test_no_unlisted_numbers_in_the_template_pin() -> None:
    """The form template keeps exactly one csrf_token field and one file input."""
    template = (
        Path(web_frames.__file__).parents[1]
        / "templates"
        / "_partials"
        / "frames_import_form.html"
    ).read_text()
    assert len(re.findall(r'name="csrf_token"', template)) == 1
    assert len(re.findall(r'name="files"', template)) == 1


class TestUrlencodedBodiesAreNotParsedOnUploadPaths:
    """#217: a form body to an upload path is refused unread, role aside."""

    @pytest.mark.parametrize(
        "path",
        ["/projects/{id}/frames/import", "/api/v1/projects/{id}/frames/upload"],
        ids=["import", "upload"],
    )
    @pytest.mark.parametrize(("size", "expected"), [(64, 403), (8 * 1024 * 1024, 413)])
    def test_viewer_urlencoded_post(
        self,
        viewer_client: TestClient,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
        size: int,
        expected: int,
    ) -> None:
        import timelapse_manager.web.dependencies as deps

        project_id = _seed_project("urlenc")
        reads: list[int] = []
        real = deps.read_body_limited

        async def spy(request: Any, limit: int) -> bytes:
            reads.append(limit)
            return await real(request, limit)

        monkeypatch.setattr(deps, "read_body_limited", spy)
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if path.startswith("/api/"):
            # The API is bearer-only (#261): the viewer's cookie only counts
            # alongside a bearer, with its CSRF token.
            headers["Authorization"] = (
                f"Bearer {ensure_local_token(get_context().settings)}"
            )
            headers["X-CSRF-Token"] = csrf_of(viewer_client, "/")
        resp = viewer_client.post(
            path.format(id=project_id),
            content=b"a=" + b"x" * size,
            headers=headers,
        )
        assert resp.status_code == expected
        assert reads == []  # refused without reading the body

    def test_limit_table_gives_urlencoded_the_form_cap(self) -> None:
        from timelapse_manager.config.settings import Settings

        settings = Settings()
        urlencoded = "application/x-www-form-urlencoded"
        for path in (
            "/projects/1/frames/import",
            "/api/v1/projects/1/frames/import",
            "/api/v1/projects/1/frames/upload",
        ):
            assert (
                body_limit.body_limit_for("POST", path, urlencoded, settings)
                == body_limit.URLENCODED_FORM_MAX_BYTES
            )
        assert (
            body_limit.body_limit_for(
                "POST", "/projects/1/frames/import", _CT, settings
            )
            == settings.server.max_request_body_bytes
        )


class TestSpoolFloorAndEmptyParts:
    def _api(self, client: TestClient, project_id: int, body: bytes) -> Any:
        token = ensure_local_token(get_context().settings)
        return client.post(
            f"/api/v1/projects/{project_id}/frames/import",
            content=body,
            headers={"Content-Type": _CT, "Authorization": f"Bearer {token}"},
        )

    def test_1000_empty_file_parts_are_422(self, web_client: TestClient) -> None:
        project_id = _seed_project("empties")
        body = _form(*[_part("files", b"", f"{i}.jpg") for i in range(1000)])
        resp = self._api(web_client, project_id, body)
        assert resp.status_code == 422
        assert _spool_entries() == []

    def test_free_space_dropping_during_the_upload_is_507(
        self, web_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from types import SimpleNamespace

        calls = 0

        def usage(path: object) -> SimpleNamespace:
            nonlocal calls
            calls += 1
            # The up-front check passes; the first flush finds the disk full.
            return SimpleNamespace(free=1 << 40 if calls <= 1 else 0)

        monkeypatch.setattr("timelapse_manager.web.multipart.shutil.disk_usage", usage)
        project_id = _seed_project("floor")
        resp = self._api(
            web_client, project_id, _form(_part("files", _jpeg(), "a.jpg"))
        )
        assert resp.status_code == 507
        assert _frame_count(project_id) == 0
        assert _spool_entries() == []
