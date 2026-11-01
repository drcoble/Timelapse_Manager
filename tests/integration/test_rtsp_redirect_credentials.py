"""RTSP credentials are not sent to another origin an RTSP 302 points at (#284).

ffmpeg follows an RTSP ``3xx`` and takes the new URL from the ``Location``
header. The credentials come from the original URL's userinfo, so a redirect to
a different host or port must not carry them (FFmpeg's ``rtsp_url_same_origin``
clears them, and a ``Location`` normally has no userinfo of its own). This runs
the real ffmpeg, with the exact argv :func:`build_ffmpeg_command` produces,
against two local stub RTSP servers:

* origin A answers ``DESCRIBE`` with ``401`` (a Basic challenge), and when the
  retry carries the login answers ``302`` to origin B (another port);
* origin B records every byte it receives and answers ``404``.

The test asserts that A really received the login (so the check is not vacuous)
and that B never saw the login in any form. It needs a real ffmpeg on ``PATH``
and is skipped without one, like the other real-ffmpeg tests; the stub's own
behaviour is covered by a test that runs everywhere.
"""

from __future__ import annotations

import asyncio
import base64
import shutil

import pytest

from timelapse_manager.cameras.rtsp import build_ffmpeg_command

_FFMPEG = shutil.which("ffmpeg")
_USER = "camuser"
_SECRET = "s3cr3t-pw"
_BASIC = base64.b64encode(f"{_USER}:{_SECRET}".encode()).decode()


class _Origin:
    """A stub RTSP server: records each request and answers from a handler."""

    def __init__(self, redirect_to: str | None, require_login: bool) -> None:
        self.redirect_to = redirect_to
        self.require_login = require_login
        self.received: list[bytes] = []
        self.port = 0
        self._server: asyncio.Server | None = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    @property
    def raw(self) -> bytes:
        return b"".join(self.received)

    def _respond(self, request: bytes) -> bytes:
        text = request.decode("latin-1")
        method = text.split(" ", 1)[0]
        cseq = next(
            (
                line.split(":", 1)[1].strip()
                for line in text.split("\r\n")
                if line.lower().startswith("cseq:")
            ),
            "1",
        )
        head = f"CSeq: {cseq}\r\n"
        if method == "OPTIONS":
            return (
                f"RTSP/1.0 200 OK\r\n{head}Public: OPTIONS, DESCRIBE\r\n\r\n".encode()
            )
        if method == "DESCRIBE":
            has_login = f"Authorization: Basic {_BASIC}" in text
            if self.require_login and not has_login:
                challenge = 'WWW-Authenticate: Basic realm="stub"\r\n'
                return f"RTSP/1.0 401 Unauthorized\r\n{head}{challenge}\r\n".encode()
            if self.redirect_to is not None:
                where = f"Location: {self.redirect_to}\r\n"
                return f"RTSP/1.0 302 Found\r\n{head}{where}\r\n".encode()
        return f"RTSP/1.0 404 Not Found\r\n{head}\r\n".encode()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            while True:
                request = await reader.readuntil(b"\r\n\r\n")
                self.received.append(request)
                writer.write(self._respond(request))
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def _origins() -> tuple[_Origin, _Origin]:
    other = _Origin(redirect_to=None, require_login=False)
    await other.start()
    first = _Origin(
        redirect_to=f"rtsp://127.0.0.1:{other.port}/redirected", require_login=True
    )
    await first.start()
    return first, other


async def test_the_stub_challenges_then_redirects() -> None:
    """Runs without ffmpeg: the stub answers 401, then 302 once logged in."""
    first, other = await _origins()
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", first.port)
        writer.write(b"DESCRIBE rtsp://x/ RTSP/1.0\r\nCSeq: 1\r\n\r\n")
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"RTSP/1.0 401")
        login = f"Authorization: Basic {_BASIC}\r\n"
        writer.write(f"DESCRIBE rtsp://x/ RTSP/1.0\r\nCSeq: 2\r\n{login}\r\n".encode())
        answer = await reader.readuntil(b"\r\n\r\n")
        assert answer.startswith(b"RTSP/1.0 302")
        assert f"127.0.0.1:{other.port}".encode() in answer
        writer.close()
    finally:
        await first.stop()
        await other.stop()


@pytest.mark.slow
@pytest.mark.skipif(_FFMPEG is None, reason="needs a real ffmpeg on PATH")
async def test_a_cross_origin_redirect_does_not_receive_the_login() -> None:
    assert _FFMPEG is not None
    first, other = await _origins()
    try:
        url = f"rtsp://{_USER}:{_SECRET}@127.0.0.1:{first.port}/stream"
        process = await asyncio.create_subprocess_exec(
            *build_ffmpeg_command(url, "tcp", 5.0, ffmpeg_binary=_FFMPEG),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(process.communicate(), timeout=60)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        # Not vacuous: the first origin really got the login after its challenge.
        assert f"Authorization: Basic {_BASIC}".encode() in first.raw
        # ffmpeg followed the redirect to the other origin ...
        assert b"RTSP/1.0" in other.raw
        # ... and sent it no login in any form.
        assert b"Authorization" not in other.raw
        assert _BASIC.encode() not in other.raw
        assert _SECRET.encode() not in other.raw
    finally:
        await first.stop()
        await other.stop()
