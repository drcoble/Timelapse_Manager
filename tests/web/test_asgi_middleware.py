"""The web middleware stack is pure ASGI (#67 P4, WP-X-A).

``BaseHTTPMiddleware`` ran each layer's inner app in a separate task and piped
every response message through an in-memory stream. The rewrite awaits the inner
app directly, so these tests pin the properties that motivated it, driven with
raw ASGI ``scope``/``receive``/``send`` (``TestClient`` and httpx's
``ASGITransport`` both buffer the whole response, so they cannot show streaming):

* a streamed render range reaches the server one chunk at a time, with the
  security headers and ``X-Request-ID`` on its start message;
* a client disconnect mid-stream ends the request promptly, closes the file
  generator and leaves no request id behind;
* the CSRF layer's body read is replayed to the endpoint, which can read the raw
  body again;
* every layer passes non-HTTP scopes (lifespan) straight through;
* :func:`apply_security_headers` (for a 500 handler outside the stack) stamps
  exactly what the middleware does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio import wait_all_tasks_blocked
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tests.conftest import csrf_of
from tests.presession_support import session_cookie
from tests.web.test_render_card import _seed_project
from tests.web.test_render_playback import _VP9, _seed_render
from timelapse_manager.api import renders as render_api
from timelapse_manager.logging import request_id_ctx
from timelapse_manager.runtime import get_context
from timelapse_manager.security.authz import prefixed_cookie_name
from timelapse_manager.web.middleware import (
    CsrfMiddleware,
    EffectiveSchemeMiddleware,
    FirstRunGateMiddleware,
    HttpsRedirectMiddleware,
    _BodyRecorder,
)
from timelapse_manager.web.request_id import RequestIdMiddleware
from timelapse_manager.web.security_headers import (
    SecurityHeadersMiddleware,
    apply_security_headers,
)

# Generous: each step is sub-millisecond; the bound only turns a deadlock (a
# buffering layer) into a failure instead of a hang.
_STEP_TIMEOUT_S = 10.0

_GATED_CHUNKS = (b"first-chunk", b"second-chunk")


def _scope(
    client: TestClient,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
    *,
    spec_version: str = "2.3",
) -> Scope:
    """Build an HTTP scope shaped like the one ``TestClient`` sends.

    The session cookie is copied from the client's jar so the request is
    authenticated as that client's user. ``spec_version`` 2.3 is what uvicorn's
    h11 protocol reports (Starlette then listens for ``http.disconnect`` in a
    task); 2.4 makes Starlette rely on ``send`` raising ``OSError`` instead.
    """
    settings = get_context().settings.session
    cookie = session_cookie(client)
    assert cookie is not None, "client is not logged in"
    cookie_name = prefixed_cookie_name(settings.cookie_name)
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [
            (b"host", b"testserver"),
            (b"cookie", f"{cookie_name}={cookie}".encode()),
            *headers,
        ],
        "client": ("testclient", 50000),
        "server": ("testserver", 443),
        "extensions": {"http.response.debug": {}},
        "state": dict(client.app_state),
    }


def _fastapi(client: TestClient) -> FastAPI:
    """Return the client's app as the FastAPI instance, to add a test route."""
    app = client.app
    assert isinstance(app, FastAPI)
    return app


def _run(client: TestClient, fn: Callable[[], Awaitable[None]]) -> None:
    """Run ``fn`` on the client's event loop (where the lifespan objects live)."""
    assert client.portal is not None, "use the client inside its context manager"
    client.portal.call(fn)


def _start(messages: list[Message]) -> dict[str, str]:
    """Return the start message's headers as a lower-cased dict."""
    start = next(m for m in messages if m["type"] == "http.response.start")
    return {k.decode().lower(): v.decode() for k, v in start["headers"]}


def _body_chunks(messages: list[Message]) -> list[bytes]:
    """Return the non-empty body chunks sent, in order."""
    return [
        m["body"]
        for m in messages
        if m["type"] == "http.response.body" and m.get("body")
    ]


class _GatedFileRange:
    """Stand-in for ``_iter_file_range`` that yields two chunks under test control.

    It yields the first chunk, then waits for ``release`` before the second, so
    the test can prove the first chunk reached the server while the body was
    still incomplete. ``closed`` records that the generator's ``finally`` ran.
    """

    def __init__(self) -> None:
        self.release = anyio.Event()
        self.closed = False

    def __call__(self, path: Path, start: int, length: int) -> AsyncIterator[bytes]:
        del path, start, length

        async def gen() -> AsyncIterator[bytes]:
            try:
                yield _GATED_CHUNKS[0]
                await self.release.wait()
                yield _GATED_CHUNKS[1]
            finally:
                self.closed = True

        return gen()


@pytest.fixture()
def gated(monkeypatch: pytest.MonkeyPatch) -> _GatedFileRange:
    """Patch the render range streamer with a gated generator."""
    fake = _GatedFileRange()
    monkeypatch.setattr(render_api, "_iter_file_range", fake)
    return fake


def _seed_streamable_render() -> int:
    """Seed a done render with a file and return its id."""
    return _seed_render(_seed_project(), settings=_VP9, streamable=False, size=4096)


class TestStreamedRangeResponse:
    """A streamed render range passes through every layer unbuffered."""

    def test_first_chunk_arrives_before_the_body_completes(
        self, viewer_client: TestClient, gated: _GatedFileRange
    ) -> None:
        render_id = _seed_streamable_render()
        scope = _scope(
            viewer_client,
            "GET",
            f"/renders/{render_id}/stream",
            [(b"range", b"bytes=0-99"), (b"x-request-id", b"stream-rid-1")],
        )
        app = viewer_client.app
        messages: list[Message] = []
        disconnect = anyio.Event()
        request_sent = False

        async def receive() -> Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                # Only now -- with the first chunk on the wire -- may the
                # generator produce the rest. A buffering layer would wait for
                # the whole body first and deadlock here.
                gated.release.set()

        async def drive() -> None:
            with anyio.fail_after(_STEP_TIMEOUT_S):
                await app(scope, receive, send)

        _run(viewer_client, drive)

        assert _body_chunks(messages) == list(_GATED_CHUNKS)
        first_body = next(
            m for m in messages if m["type"] == "http.response.body" and m.get("body")
        )
        assert first_body["more_body"] is True
        headers = _start(messages)
        assert messages[0]["status"] == 206
        assert headers["x-request-id"] == "stream-rid-1"
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["x-frame-options"] == "DENY"
        assert "content-security-policy" in headers
        assert headers["strict-transport-security"].startswith("max-age=")
        assert gated.closed

    def test_producer_runs_no_further_ahead_than_the_server_reads(
        self, viewer_client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Backpressure is end to end: no layer holds chunks of its own.

        The server stalls on the first body chunk. With every layer awaiting the
        inner app directly, the generator cannot advance past that chunk; each
        ``BaseHTTPMiddleware`` layer used to let it run one chunk further ahead
        (one hand-off per in-memory stream), so the stack held several chunks.
        """
        produced = 0

        def counting(path: Path, start: int, length: int) -> AsyncIterator[bytes]:
            del path, start, length

            async def gen() -> AsyncIterator[bytes]:
                nonlocal produced
                for i in range(20):
                    produced += 1
                    yield f"chunk-{i:02d}".encode()

            return gen()

        monkeypatch.setattr(render_api, "_iter_file_range", counting)
        render_id = _seed_streamable_render()
        scope = _scope(
            viewer_client,
            "GET",
            f"/renders/{render_id}/stream",
            [(b"range", b"bytes=0-99")],
        )
        app = viewer_client.app
        stalled = anyio.Event()
        resume = anyio.Event()
        produced_while_stalled: list[int] = []
        chunks: list[bytes] = []
        request_sent = False

        async def receive() -> Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        async def send(message: Message) -> None:
            if message["type"] == "http.response.body" and message.get("body"):
                chunks.append(message["body"])
                if not stalled.is_set():
                    stalled.set()
                    await resume.wait()  # a slow client: the socket is full

        async def drive() -> None:
            with anyio.fail_after(_STEP_TIMEOUT_S):
                async with anyio.create_task_group() as tg:
                    tg.start_soon(app, scope, receive, send)
                    await stalled.wait()
                    # Let every other task run until all are blocked.
                    await wait_all_tasks_blocked()
                    produced_while_stalled.append(produced)
                    resume.set()

        _run(viewer_client, drive)

        assert produced_while_stalled == [1]
        assert len(chunks) == 20

    def test_endpoint_cache_control_passes_through_unchanged(
        self, viewer_client: TestClient
    ) -> None:
        """A ``Cache-Control`` an endpoint sets survives every layer on a stream."""

        async def chunks() -> AsyncIterator[bytes]:
            yield b"a"
            yield b"b"

        async def cached_stream() -> StreamingResponse:
            return StreamingResponse(
                chunks(),
                media_type="application/octet-stream",
                headers={"Cache-Control": "private, max-age=60"},
            )

        _fastapi(viewer_client).add_api_route("/__wpxa/cached-stream", cached_stream)

        resp = viewer_client.get("/__wpxa/cached-stream")

        assert resp.status_code == 200
        assert resp.content == b"ab"
        assert resp.headers["cache-control"] == "private, max-age=60"
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers.get("x-request-id")


class TestClientDisconnect:
    """A client that goes away mid-stream ends the request promptly."""

    def test_disconnect_message_cancels_the_stream(
        self, viewer_client: TestClient, gated: _GatedFileRange
    ) -> None:
        """ASGI spec < 2.4 (uvicorn h11): Starlette listens for ``http.disconnect``.

        The disconnect arrives while the generator is parked between chunks; it
        must reach the response's listener through every layer and cancel the
        generator rather than leave it waiting forever.
        """
        render_id = _seed_streamable_render()
        scope = _scope(
            viewer_client,
            "GET",
            f"/renders/{render_id}/stream",
            [(b"range", b"bytes=0-99")],
            spec_version="2.3",
        )
        app = viewer_client.app
        messages: list[Message] = []
        gone = anyio.Event()
        request_sent = False
        ctx_after: list[str | None] = []

        async def receive() -> Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                gone.set()  # the client drops after the first chunk

        async def drive() -> None:
            with anyio.fail_after(_STEP_TIMEOUT_S):
                await app(scope, receive, send)
            ctx_after.append(request_id_ctx.get())

        _run(viewer_client, drive)

        assert _body_chunks(messages) == [_GATED_CHUNKS[0]]
        assert gated.closed, "the file generator was not closed on disconnect"
        assert ctx_after == [None]

    def test_failed_send_unwinds_the_request(
        self, viewer_client: TestClient, gated: _GatedFileRange
    ) -> None:
        """ASGI spec >= 2.4: a server reports a gone client by ``send`` raising.

        The error must propagate out through every layer (no layer may swallow it
        or keep streaming), and the request id must not outlive the request.
        """
        render_id = _seed_streamable_render()
        scope = _scope(
            viewer_client,
            "GET",
            f"/renders/{render_id}/stream",
            [(b"range", b"bytes=0-99")],
            spec_version="2.4",
        )
        app = viewer_client.app
        messages: list[Message] = []
        sends_after_gone = 0
        request_sent = False
        outcome: list[BaseException | None] = []
        ctx_after: list[str | None] = []

        async def receive() -> Message:
            nonlocal request_sent
            if not request_sent:
                request_sent = True
                return {"type": "http.request", "body": b"", "more_body": False}
            await anyio.sleep_forever()
            raise AssertionError("unreachable")

        async def send(message: Message) -> None:
            nonlocal sends_after_gone
            if gated.release.is_set():
                sends_after_gone += 1
                raise OSError("client disconnected")
            messages.append(message)
            if message["type"] == "http.response.body" and message.get("body"):
                gated.release.set()  # the client drops after the first chunk

        async def drive() -> None:
            with anyio.fail_after(_STEP_TIMEOUT_S):
                try:
                    await app(scope, receive, send)
                except Exception as exc:  # noqa: BLE001 - recorded and asserted
                    outcome.append(exc)
                else:
                    outcome.append(None)
            ctx_after.append(request_id_ctx.get())

        _run(viewer_client, drive)

        assert _body_chunks(messages) == [_GATED_CHUNKS[0]]
        assert outcome and outcome[0] is not None, "the disconnect was swallowed"
        # The second chunk was attempted once; nothing kept streaming after it.
        assert sends_after_gone >= 1
        assert ctx_after == [None]


class TestCsrfBodyReplay:
    """The CSRF layer's form read is handed back to the endpoint intact."""

    def test_endpoint_reads_the_raw_body_after_a_form_token_check(
        self, admin_client: TestClient
    ) -> None:
        async def echo(request: Request) -> JSONResponse:
            # Deliberately not ``parsed_form``: the raw stream must be replayed,
            # not just the ``request.state`` cache.
            request.state.parsed_form = None
            body = await request.body()
            return JSONResponse({"body": body.decode()})

        _fastapi(admin_client).add_api_route("/__wpxa/echo", echo, methods=["POST"])
        csrf = csrf_of(admin_client, "/")
        payload = f"csrf_token={csrf}&name=kept&n=42"

        resp = admin_client.post(
            "/__wpxa/echo",
            content=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"body": payload}

    def test_endpoint_reads_the_raw_multipart_body(
        self, admin_client: TestClient
    ) -> None:
        async def echo(request: Request) -> JSONResponse:
            body = await request.body()
            return JSONResponse({"size": len(body), "has_file": b"PAYLOAD" in body})

        _fastapi(admin_client).add_api_route("/__wpxa/upload", echo, methods=["POST"])
        csrf = csrf_of(admin_client, "/")
        boundary = "wpxaboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="csrf_token"\r\n\r\n'
            f"{csrf}\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="f.bin"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
            "PAYLOAD\r\n"
            f"--{boundary}--\r\n"
        ).encode()

        resp = admin_client.post(
            "/__wpxa/upload",
            content=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

        assert resp.status_code == 200
        assert resp.json() == {"size": len(body), "has_file": True}

    def test_bad_form_token_still_gets_the_same_403(
        self, admin_client: TestClient
    ) -> None:
        resp = admin_client.post(
            "/logout",
            data={"csrf_token": "wrong"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            follow_redirects=False,
        )

        assert resp.status_code == 403
        assert resp.text == "CSRF validation failed."
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert resp.headers.get("x-request-id")


class TestBodyRecorder:
    """Replay hands back recorded messages, then defers to the server."""

    def test_replays_recorded_messages_then_delegates(self) -> None:
        upstream: list[Message] = [
            {"type": "http.request", "body": b"ab", "more_body": True},
            {"type": "http.request", "body": b"cd", "more_body": False},
            {"type": "http.disconnect"},
        ]

        async def receive() -> Message:
            return upstream.pop(0)

        async def scenario() -> list[Message]:
            recorder = _BodyRecorder(receive)
            await recorder.receive()
            await recorder.receive()
            replay = recorder.replaying_receive()
            return [await replay(), await replay(), await replay()]

        seen = anyio.run(scenario)

        assert [m.get("body") for m in seen[:2]] == [b"ab", b"cd"]
        # The disconnect comes from the server, not from the recorder.
        assert seen[2] == {"type": "http.disconnect"}

    def test_nothing_recorded_returns_the_original_receive(self) -> None:
        async def receive() -> Message:
            return {"type": "http.disconnect"}

        assert _BodyRecorder(receive).replaying_receive() is receive


_LAYERS: list[Callable[[ASGIApp], ASGIApp]] = [
    RequestIdMiddleware,
    EffectiveSchemeMiddleware,
    SecurityHeadersMiddleware,
    HttpsRedirectMiddleware,
    FirstRunGateMiddleware,
    CsrfMiddleware,
]


@pytest.mark.parametrize("layer", _LAYERS, ids=lambda cls: cls.__name__)
def test_non_http_scopes_pass_straight_through(
    layer: Callable[[ASGIApp], ASGIApp],
) -> None:
    """Lifespan traffic reaches the app untouched (no context, no DB)."""
    seen: list[tuple[Scope, Receive, Send]] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append((scope, receive, send))

    async def receive() -> Message:
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:
        del message

    scope: dict[str, Any] = {"type": "lifespan", "state": {}}
    anyio.run(layer(inner), scope, receive, send)

    assert seen == [(scope, receive, send)]
    assert request_id_ctx.get() is None


def _request(scheme: str, effective: str | None) -> Request:
    """A bare request whose ``state`` optionally carries an effective scheme."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": "GET",
        "scheme": scheme,
        "path": "/",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 443 if scheme == "https" else 80),
        "state": {},
    }
    if effective is not None:
        scope["state"]["effective_scheme"] = effective
    return Request(scope)


class TestApplySecurityHeaders:
    """The exported helper and the middleware stamp one header set."""

    def test_effective_https_adds_hsts(self) -> None:
        response = Response("x")
        apply_security_headers(_request("http", "https"), response)

        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert "geolocation=()" in response.headers["permissions-policy"]
        assert "default-src 'self'" in response.headers["content-security-policy"]
        assert response.headers["strict-transport-security"].startswith("max-age=")

    def test_effective_http_has_no_hsts(self) -> None:
        response = Response("x")
        apply_security_headers(_request("https", "http"), response)

        assert response.headers["x-frame-options"] == "DENY"
        assert "strict-transport-security" not in response.headers

    def test_falls_back_to_the_connection_scheme(self) -> None:
        response = Response("x")
        apply_security_headers(_request("https", None), response)

        assert "strict-transport-security" in response.headers

    def test_replaces_an_existing_value(self) -> None:
        response = Response("x", headers={"X-Frame-Options": "SAMEORIGIN"})
        apply_security_headers(_request("https", "https"), response)

        assert response.headers.getlist("x-frame-options") == ["DENY"]

    def test_matches_what_the_middleware_sends(self, admin_client: TestClient) -> None:
        via_stack = admin_client.get("/healthz")
        direct = Response("x")
        apply_security_headers(_request("https", "https"), direct)

        names = [
            "x-content-type-options",
            "x-frame-options",
            "referrer-policy",
            "permissions-policy",
            "content-security-policy",
            "strict-transport-security",
        ]
        assert {n: via_stack.headers[n] for n in names} == {
            n: direct.headers[n] for n in names
        }
