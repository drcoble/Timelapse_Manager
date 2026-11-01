"""Tests for the request-id middleware, context var, and logging filter."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response, StreamingResponse
from fastapi.testclient import TestClient

from timelapse_manager.logging import RequestIdFilter, request_id_ctx
from timelapse_manager.web.request_id import RequestIdMiddleware


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/x")
    def x(request: Request) -> dict[str, str | None]:
        return {
            "state": getattr(request.state, "request_id", None),
            "ctx": request_id_ctx.get(),
        }

    return app


class TestRequestIdMiddleware:
    """The middleware assigns, propagates, and echoes the correlation id."""

    def test_generates_id_when_header_absent(self) -> None:
        response = TestClient(_app()).get("/x")
        rid = response.headers.get("X-Request-ID")
        assert rid is not None and rid != ""

    def test_honours_inbound_header(self) -> None:
        response = TestClient(_app()).get("/x", headers={"X-Request-ID": "fixed-123"})
        assert response.headers["X-Request-ID"] == "fixed-123"

    def test_rejects_oversized_or_malformed_inbound_id(self) -> None:
        """An untrusted client cannot reflect an arbitrary/oversized id.

        A too-long or non-token value is replaced with a fresh generated id
        rather than echoed back into the response header and the logs.
        """
        for bad in ("x" * 200, "has spaces", "semi;colon", "slash/y"):
            response = TestClient(_app()).get("/x", headers={"X-Request-ID": bad})
            echoed = response.headers["X-Request-ID"]
            assert echoed != bad
            assert len(echoed) == 32 and echoed.isalnum()

    def test_id_is_on_request_state(self) -> None:
        response = TestClient(_app()).get("/x", headers={"X-Request-ID": "fixed-abc"})
        assert response.json()["state"] == "fixed-abc"

    def test_context_var_visible_in_handler(self) -> None:
        """The context var carries the id into the endpoint's execution."""
        response = TestClient(_app()).get("/x", headers={"X-Request-ID": "ctx-99"})
        assert response.json()["ctx"] == "ctx-99"

    def test_context_var_reset_after_request(self) -> None:
        """No id leaks to the next task once the request completes."""
        TestClient(_app()).get("/x", headers={"X-Request-ID": "leak-check"})
        assert request_id_ctx.get() is None


class TestRequestIdFilter:
    """The filter stamps the current id onto records without dropping any."""

    def test_stamps_id_when_set(self) -> None:
        token = request_id_ctx.set("rec-1")
        try:
            record = logging.makeLogRecord({"msg": "hi"})
            assert RequestIdFilter().filter(record) is True
            assert record.request_id == "rec-1"  # type: ignore[attr-defined]
        finally:
            request_id_ctx.reset(token)

    def test_no_id_leaves_record_untouched(self) -> None:
        record = logging.makeLogRecord({"msg": "hi"})
        assert RequestIdFilter().filter(record) is True
        assert not hasattr(record, "request_id")

    def test_does_not_overwrite_existing_id(self) -> None:
        token = request_id_ctx.set("from-ctx")
        try:
            record = logging.makeLogRecord({"msg": "hi", "request_id": "explicit"})
            RequestIdFilter().filter(record)
            assert record.request_id == "explicit"  # type: ignore[attr-defined]
        finally:
            request_id_ctx.reset(token)


class TestRequestIdEndToEnd:
    """A log record emitted while handling a request carries the response's id."""

    def test_log_record_carries_request_id(self) -> None:
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        handler.addFilter(RequestIdFilter())
        log = logging.getLogger("test.request_id.e2e")
        log.handlers.clear()
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False

        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/log")
        def log_route() -> dict[str, str]:
            log.info("handling")
            return {"ok": "yes"}

        response = TestClient(app).get("/log", headers={"X-Request-ID": "trace-42"})
        rid = response.headers["X-Request-ID"]
        assert rid == "trace-42"
        assert any(getattr(record, "request_id", None) == rid for record in records)


class TestRequestIdPureAsgi:
    """The pure-ASGI middleware keeps the id on every path a request can take."""

    def test_async_endpoint_log_record_carries_request_id(self) -> None:
        """An ``async def`` endpoint runs in the middleware's own task."""
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = _Capture()
        handler.addFilter(RequestIdFilter())
        log = logging.getLogger("test.request_id.async")
        log.handlers.clear()
        log.addHandler(handler)
        log.setLevel(logging.INFO)
        log.propagate = False

        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/alog")
        async def alog_route() -> dict[str, str]:
            log.info("handling async")
            return {"ok": "yes"}

        response = TestClient(app).get("/alog", headers={"X-Request-ID": "async-7"})

        assert response.headers["X-Request-ID"] == "async-7"
        assert [getattr(r, "request_id", None) for r in records] == ["async-7"]

    def test_replaces_an_inner_x_request_id_header(self) -> None:
        """The echoed id is the middleware's, even if the endpoint set its own."""
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/dup")
        def dup() -> Response:
            return PlainTextResponse("x", headers={"X-Request-ID": "inner"})

        response = TestClient(app).get("/dup", headers={"X-Request-ID": "outer-1"})

        assert response.headers.get_list("X-Request-ID") == ["outer-1"]

    def test_streamed_response_carries_the_id(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        async def chunks() -> AsyncIterator[bytes]:
            yield b"a"
            yield b"b"

        @app.get("/stream")
        async def stream() -> StreamingResponse:
            return StreamingResponse(chunks())

        response = TestClient(app).get("/stream", headers={"X-Request-ID": "s-1"})

        assert response.content == b"ab"
        assert response.headers["X-Request-ID"] == "s-1"

    def test_context_var_reset_when_the_endpoint_raises(self) -> None:
        app = FastAPI()
        app.add_middleware(RequestIdMiddleware)

        @app.get("/boom")
        async def boom() -> None:
            raise RuntimeError("boom")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/boom", headers={"X-Request-ID": "boom-1"})

        assert response.status_code == 500
        assert request_id_ctx.get() is None
