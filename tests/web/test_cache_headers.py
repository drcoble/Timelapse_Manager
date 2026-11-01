"""Default ``Cache-Control: no-store`` and ``Vary: HX-Request`` (#70 F-S2, F-R11).

* HTML and JSON responses that set no ``Cache-Control`` get ``no-store``: pages,
  partials, the JSON polls, the API, the 401 JSON and ``/healthz``.
* Responses that chose their own policy keep it (frame image and thumbnail at
  ``private, max-age=86400``, owner decision D3) and the render stream and
  ``/static/`` get no ``no-store``.
* Every HTML response carries ``Vary: HX-Request``, including the routes that
  branch on that header, merged with any existing ``Vary``.
* The middleware itself, driven as raw ASGI: non-HTTP scopes pass through,
  existing headers are respected, and streamed bodies are not buffered.
* Through the whole (pure-ASGI) stack: the render range stream arrives chunk
  by chunk with no ``Cache-Control`` added, and a streamed JSON body gets
  ``no-store`` or keeps the endpoint's own policy.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from tests.web.test_asgi_middleware import (
    _GATED_CHUNKS,
    _STEP_TIMEOUT_S,
    _body_chunks,
    _fastapi,
    _GatedFileRange,
    _run,
    _scope,
    _start,
)
from tests.web.test_frame_image import _seed_camera, _write_default_frame
from tests.web.test_frame_image import _seed_project as _seed_frame_project
from tests.web.test_render_card import _seed_project as _seed_render_project
from tests.web.test_render_playback import _AV1, _seed_render
from timelapse_manager.api import renders as render_api
from timelapse_manager.runtime import get_context
from timelapse_manager.storage import paths
from timelapse_manager.web.cache_headers import CacheControlMiddleware, merge_vary

_FRAME_CACHE = "private, max-age=86400"


def _vary_tokens(value: str | None) -> list[str]:
    return [
        token.strip().lower() for token in (value or "").split(",") if token.strip()
    ]


# ---------------------------------------------------------------------------
# no-store on dynamic HTML and JSON
# ---------------------------------------------------------------------------


class TestNoStore:
    @pytest.mark.parametrize(
        "path",
        [
            "/",
            "/partials/projects",
            "/alerts/summary",
            "/events/since?after=0&scope=camera",
        ],
    )
    def test_authenticated_html_and_json(
        self, admin_client: TestClient, path: str
    ) -> None:
        resp = admin_client.get(path)
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"

    def test_login_page(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/login")
        assert resp.headers["cache-control"] == "no-store"

    def test_api_json(self, migrated_client: TestClient, cam_auth_token: str) -> None:
        resp = migrated_client.get(
            "/api/v1/projects", headers={"Authorization": f"Bearer {cam_auth_token}"}
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.headers["cache-control"] == "no-store"

    def test_unauthenticated_401_json(self, client: TestClient) -> None:
        resp = client.get("/api/v1/projects")
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("application/json")
        assert resp.headers["cache-control"] == "no-store"

    def test_healthz(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert resp.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Explicit policies and non-HTML/JSON responses are left alone
# ---------------------------------------------------------------------------


class TestLeftAlone:
    def test_static(self, anon_client: TestClient) -> None:
        resp = anon_client.get("/static/js/selection.js")
        assert "no-store" not in resp.headers["cache-control"]
        assert "vary" not in resp.headers

    def test_frame_image_keeps_its_policy(self, admin_client: TestClient) -> None:
        pid = _seed_frame_project(name="Cache Img", camera_id=_seed_camera())
        fid = _write_default_frame(pid)
        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/image")
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == _FRAME_CACHE

    def test_thumbnail_keeps_its_policy(self, admin_client: TestClient) -> None:
        pid = _seed_frame_project(name="Cache Thumb", camera_id=_seed_camera())
        fid = _write_default_frame(pid)
        # Pre-seed a fresh cached thumbnail, so the route serves it without
        # running ffmpeg (not installed on every dev machine).
        thumb = paths.thumbnail_cache_dir(get_context().settings, pid) / f"{fid}.jpg"
        thumb.parent.mkdir(parents=True, exist_ok=True)
        thumb.write_bytes(b"\xff\xd8\xff\xd9")
        later = os.path.getmtime(thumb) + 60
        os.utime(thumb, (later, later))

        resp = admin_client.get(f"/projects/{pid}/frames/{fid}/thumbnail")
        assert resp.status_code == 200
        assert resp.content == b"\xff\xd8\xff\xd9"
        assert resp.headers["cache-control"] == _FRAME_CACHE

    def test_render_stream(self, viewer_client: TestClient) -> None:
        job_id = _seed_render(_seed_render_project(), settings=_AV1, streamable=False)
        resp = viewer_client.get(
            f"/renders/{job_id}/stream", headers={"Range": "bytes=0-9"}
        )
        assert resp.status_code == 206
        assert "no-store" not in resp.headers.get("cache-control", "")
        assert "hx-request" not in _vary_tokens(resp.headers.get("vary"))


# ---------------------------------------------------------------------------
# Vary: HX-Request on HTML
# ---------------------------------------------------------------------------


class TestVary:
    # Routes that return a full page or a partial for the same URL depending on
    # HX-Request (frames.py, events.py, renders.py, dashboard.py, users.py,
    # reports.py), plus a plain page.
    @pytest.mark.parametrize(
        "path",
        ["/frames", "/events", "/renders", "/projects", "/users", "/report", "/"],
    )
    @pytest.mark.parametrize("hx", [False, True])
    def test_branching_routes(
        self, admin_client: TestClient, path: str, hx: bool
    ) -> None:
        headers = {"HX-Request": "true"} if hx else {}
        resp = admin_client.get(path, headers=headers)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert _vary_tokens(resp.headers.get("vary")).count("hx-request") == 1

    def test_json_has_no_hx_vary(self, client: TestClient) -> None:
        resp = client.get("/healthz")
        assert "hx-request" not in _vary_tokens(resp.headers.get("vary"))


@pytest.mark.parametrize(
    ("existing", "expected"),
    [
        (None, "HX-Request"),
        ("", "HX-Request"),
        ("Accept-Encoding", "Accept-Encoding, HX-Request"),
        ("Accept-Encoding, hx-request", "Accept-Encoding, hx-request"),
        ("*", "*"),
    ],
)
def test_merge_vary(existing: str | None, expected: str) -> None:
    assert merge_vary(existing, "HX-Request") == expected


# ---------------------------------------------------------------------------
# The middleware as raw ASGI
# ---------------------------------------------------------------------------


def _app(
    headers: list[tuple[bytes, bytes]], chunks: list[bytes], log: list[str]
) -> Any:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": headers})
        for i, chunk in enumerate(chunks):
            log.append(f"app-sent-{i}")
            await send(
                {
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": i < len(chunks) - 1,
                }
            )

    return app


async def _drive(
    path: str, headers: list[tuple[bytes, bytes]]
) -> tuple[list[Message], list[str]]:
    log: list[str] = []
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            log.append("client-got")
        sent.append(message)

    middleware = CacheControlMiddleware(_app(headers, [b"a", b"b"], log))
    scope = {"type": "http", "path": path, "method": "GET", "headers": []}
    await middleware(scope, receive, send)
    return sent, log


def _headers(message: Message) -> dict[bytes, bytes]:
    return dict(message["headers"])


class TestMiddlewareAsgi:
    async def test_existing_vary_merged_and_explicit_cache_kept(self) -> None:
        sent, _ = await _drive(
            "/x",
            [
                (b"content-type", b"text/html; charset=utf-8"),
                (b"cache-control", b"private, max-age=5"),
                (b"vary", b"Accept-Encoding"),
            ],
        )
        headers = _headers(sent[0])
        assert headers[b"cache-control"] == b"private, max-age=5"
        assert headers[b"vary"] == b"Accept-Encoding, HX-Request"

    async def test_problem_json_gets_no_store(self) -> None:
        sent, _ = await _drive("/x", [(b"content-type", b"application/problem+json")])
        assert _headers(sent[0])[b"cache-control"] == b"no-store"

    async def test_other_types_untouched(self) -> None:
        sent, _ = await _drive("/x", [(b"content-type", b"video/mp4")])
        assert _headers(sent[0]) == {b"content-type": b"video/mp4"}

    async def test_static_path_untouched(self) -> None:
        sent, _ = await _drive("/static/x.html", [(b"content-type", b"text/html")])
        assert _headers(sent[0]) == {b"content-type": b"text/html"}

    async def test_body_streams_through_unbuffered(self) -> None:
        _, log = await _drive("/x", [(b"content-type", b"text/html")])
        # Each chunk reaches the client before the app produces the next one.
        assert log == ["app-sent-0", "client-got", "app-sent-1", "client-got"]

    async def test_non_http_scope_passes_through(self) -> None:
        seen: list[Scope] = []

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            seen.append(scope)

        async def receive() -> Message:
            return {"type": "lifespan.startup"}

        async def send(message: Message) -> None:
            raise AssertionError("nothing is sent")

        scope: Scope = {"type": "lifespan"}
        await CacheControlMiddleware(app)(scope, receive, send)
        assert seen == [scope]


# ---------------------------------------------------------------------------
# Streams through the full stack (after WP-X-A's pure-ASGI rewrite)
# ---------------------------------------------------------------------------


@pytest.fixture()
def gated_range(monkeypatch: pytest.MonkeyPatch) -> _GatedFileRange:
    """Patch the render range streamer with WP-X-A's gated generator."""
    fake = _GatedFileRange()
    monkeypatch.setattr(render_api, "_iter_file_range", fake)
    return fake


async def _drive_gated(
    client: TestClient, path: str, headers: list[tuple[bytes, bytes]], gate: Any
) -> list[Message]:
    """Drive the whole app with raw ASGI; open ``gate`` on the first body chunk."""
    messages: list[Message] = []
    request_sent = False

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await anyio.Event().wait()  # never disconnects
        raise AssertionError("unreachable")

    async def send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            # A buffering layer would wait for the whole body and deadlock here.
            gate.set()

    with anyio.fail_after(_STEP_TIMEOUT_S):
        await client.app(_scope(client, "GET", path, headers), receive, send)
    return messages


class TestStreamsThroughTheStack:
    def test_render_stream_is_unbuffered_and_left_alone(
        self, viewer_client: TestClient, gated_range: _GatedFileRange
    ) -> None:
        job_id = _seed_render(_seed_render_project(), settings=_AV1, streamable=False)
        messages: list[Message] = []

        async def run() -> None:
            messages.extend(
                await _drive_gated(
                    viewer_client,
                    f"/renders/{job_id}/stream",
                    [(b"range", b"bytes=0-99"), (b"x-request-id", b"wp70a-1")],
                    gated_range.release,
                )
            )

        _run(viewer_client, run)

        assert _body_chunks(messages) == list(_GATED_CHUNKS)
        headers = _start(messages)
        assert messages[0]["status"] == 206
        # The stream sets no Cache-Control and is video, so the defaults do
        # not touch it (WP-70-A, step 6); every other layer's headers are there.
        assert "cache-control" not in headers
        assert "hx-request" not in _vary_tokens(headers.get("vary"))
        assert headers["x-request-id"] == "wp70a-1"
        assert headers["x-content-type-options"] == "nosniff"
        assert "content-security-policy" in headers

    @pytest.mark.parametrize(
        ("endpoint_cache", "expected"),
        [(None, "no-store"), ("private, max-age=60", "private, max-age=60")],
    )
    def test_streamed_json_gets_the_default_or_keeps_its_own(
        self, viewer_client: TestClient, endpoint_cache: str | None, expected: str
    ) -> None:
        gate = anyio.Event()

        async def chunks() -> AsyncIterator[bytes]:
            yield b'{"a":'
            await gate.wait()
            yield b"1}"

        async def json_stream() -> StreamingResponse:
            extra = {"Cache-Control": endpoint_cache} if endpoint_cache else {}
            return StreamingResponse(
                chunks(), media_type="application/json", headers=extra
            )

        _fastapi(viewer_client).add_api_route("/__wp70a/json-stream", json_stream)
        messages: list[Message] = []

        async def run() -> None:
            messages.extend(
                await _drive_gated(viewer_client, "/__wp70a/json-stream", [], gate)
            )

        _run(viewer_client, run)

        assert _body_chunks(messages) == [b'{"a":', b"1}"]
        headers = _start(messages)
        assert headers["cache-control"] == expected
        assert headers.get("x-request-id")
