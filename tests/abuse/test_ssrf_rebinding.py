"""Outbound webhooks connect only to the address the SSRF guard validated.

Security finding on WP-67-R (ssrf-dns-rebinding): a cached "allowed" verdict
let the caller hand the URL to ``httpx``, which resolved the name again at
connect time. A rebinding host could answer with a public address for the check
and ``127.0.0.1`` or ``169.254.169.254`` for the connection.

The fix pins the connection: the transport's network backend asks the guard for
a validated IP at TCP-connect time and connects to exactly that IP, keeping the
hostname for TLS SNI and the ``Host`` header. These tests record every connect
address through a fake socket backend, so no real network is used.
"""

from __future__ import annotations

import socket
import typing
from collections.abc import Iterator
from datetime import UTC, datetime
from unittest.mock import MagicMock

import httpcore
import pytest

from timelapse_manager.config.settings import ReportingSettings, Settings
from timelapse_manager.monitoring.channels import ChannelSendError, NotificationMessage
from timelapse_manager.monitoring.channels.webhook import WebhookChannel
from timelapse_manager.render import post_actions
from timelapse_manager.reports import delivery
from timelapse_manager.security import pinned_http, ssrf
from timelapse_manager.security.pinned_http import PinnedNetworkBackend
from timelapse_manager.security.ssrf import (
    SsrfError,
    assert_allowed_url_async,
    clear_resolution_cache,
    pinned_address_async,
)

pytestmark = pytest.mark.abuse

_RESOLVER = "timelapse_manager.security.ssrf.socket.getaddrinfo"
_PUBLIC = "93.184.216.34"


class _RebindingResolver:
    """Answers each lookup of a name with the next address in its script."""

    def __init__(self, *answers: str) -> None:
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, host: str, *args: object, **kwargs: object) -> list:
        index = min(self.calls, len(self.answers) - 1)
        self.calls += 1
        return [(None, None, None, "", (self.answers[index], 0))]


class _FakeStream(httpcore.AsyncNetworkStream):
    """Answers any request with 204 and records what was sent."""

    def __init__(self) -> None:
        self.written = b""
        self.sni: str | None = None
        self._pending = b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        chunk, self._pending = self._pending, b""
        return chunk

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self.written += buffer

    async def aclose(self) -> None:
        return None

    async def start_tls(
        self,
        ssl_context: typing.Any,
        server_hostname: str | None = None,
        timeout: float | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self.sni = server_hostname
        return self

    def get_extra_info(self, info: str) -> typing.Any:
        return None


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """Records each connect address and hands back a fake stream."""

    def __init__(self) -> None:
        self.connects: list[tuple[str, int]] = []
        self.stream = _FakeStream()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Any = None,
    ) -> httpcore.AsyncNetworkStream:
        self.connects.append((host, port))
        return self.stream

    async def sleep(self, seconds: float) -> None:
        return None


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clear_resolution_cache()
    yield
    clear_resolution_cache()


@pytest.fixture
def sockets(monkeypatch: pytest.MonkeyPatch) -> _RecordingBackend:
    backend = _RecordingBackend()
    monkeypatch.setattr(pinned_http, "inner_backend_factory", lambda: backend)
    return backend


def _message() -> NotificationMessage:
    return NotificationMessage(
        event_type="render.failed",
        scope="system",
        scope_id=None,
        level="error",
        message="m",
        timestamp=datetime.now(UTC).replace(tzinfo=None),
        metadata=None,
    )


class TestPinnedBackend:
    @pytest.mark.parametrize("second", ["127.0.0.1", "169.254.169.254"])
    async def test_rebinding_after_the_check_reaches_the_checked_address(
        self, monkeypatch: pytest.MonkeyPatch, second: str
    ) -> None:
        resolver = _RebindingResolver(_PUBLIC, second)
        monkeypatch.setattr(_RESOLVER, resolver)
        inner = _RecordingBackend()
        backend = PinnedNetworkBackend(inner=inner)

        await assert_allowed_url_async("https://hook.example.test/")
        await backend.connect_tcp("hook.example.test", 443)

        assert inner.connects == [(_PUBLIC, 443)]

    @pytest.mark.parametrize("second", ["127.0.0.1", "169.254.169.254"])
    async def test_rebinding_after_the_cache_expires_is_blocked(
        self, monkeypatch: pytest.MonkeyPatch, second: str
    ) -> None:
        resolver = _RebindingResolver(_PUBLIC, second)
        monkeypatch.setattr(_RESOLVER, resolver)
        inner = _RecordingBackend()
        backend = PinnedNetworkBackend(inner=inner)

        await assert_allowed_url_async("https://hook.example.test/")
        clear_resolution_cache()  # as if the 60 s TTL ran out before the send
        with pytest.raises(httpcore.ConnectError, match="SSRF"):
            await backend.connect_tcp("hook.example.test", 443)

        assert inner.connects == []
        assert resolver.calls == 2

    async def test_falls_back_to_the_next_validated_address(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dual-stack host whose first answer refuses still connects."""

        def answers(host: str, *args: object, **kwargs: object) -> list:
            return [
                (None, None, None, "", ("2606:2800:220:1::1", 0)),
                (None, None, None, "", (_PUBLIC, 0)),
            ]

        monkeypatch.setattr(_RESOLVER, answers)

        class _FirstRefuses(_RecordingBackend):
            async def connect_tcp(self, host: str, port: int, **kwargs: typing.Any):  # type: ignore[override]
                self.connects.append((host, port))
                if len(self.connects) == 1:
                    raise httpcore.ConnectError("network unreachable")
                return self.stream

        inner = _FirstRefuses()
        await PinnedNetworkBackend(inner=inner).connect_tcp("hook.example.test", 443)
        assert inner.connects == [("2606:2800:220:1::1", 443), (_PUBLIC, 443)]

    async def test_unix_sockets_are_refused(self) -> None:
        backend = PinnedNetworkBackend(inner=_RecordingBackend())
        with pytest.raises(httpcore.ConnectError):
            await backend.connect_unix_socket("/run/docker.sock")


class TestParserDifferentials:
    @pytest.mark.parametrize(
        "host",
        [
            "2130706433",  # decimal 127.0.0.1
            "0177.0.0.1",  # octal 127.0.0.1 (glibc); 177.0.0.1 on macOS
            "0x7f.0.0.1",  # hex 127.0.0.1
            "127.1",  # short form
            "[::ffff:127.0.0.1]",  # bracketed IPv4-mapped IPv6
            "::ffff:169.254.169.254",
            "fe80::1%eth0",  # zone id
            "[::1]",
            "0.0.0.0",
        ],
    )
    async def test_alternate_spellings_never_yield_a_denied_connect_address(
        self, host: str
    ) -> None:
        # The real resolver. Platforms read these spellings differently (macOS
        # takes ``0177.0.0.1`` as decimal 177.0.0.1, glibc as octal 127.0.0.1),
        # which is why the guard never interprets the spelling itself: it
        # validates the resolver's answer, and that answer is the exact address
        # the pinned socket connects to. So either the host is refused, or the
        # address returned for the connection is itself allowed.
        try:
            address = await pinned_address_async(host)
        except SsrfError:
            return
        ssrf.assert_address_allowed(address)

    @pytest.mark.parametrize(
        ("host", "answer"),
        [("0177.0.0.1", "127.0.0.1"), ("2130706433", "127.0.0.1")],
    )
    async def test_glibc_style_numeric_reading_is_denied(
        self, monkeypatch: pytest.MonkeyPatch, host: str, answer: str
    ) -> None:
        monkeypatch.setattr(_RESOLVER, _RebindingResolver(answer))
        with pytest.raises(SsrfError):
            await pinned_address_async(host)

    async def test_connection_goes_to_the_validated_ip_not_the_spelling(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_RESOLVER, _RebindingResolver(_PUBLIC))
        inner = _RecordingBackend()
        await PinnedNetworkBackend(inner=inner).connect_tcp("HOOK.example.test.", 80)
        assert inner.connects == [(_PUBLIC, 80)]


class TestWebhookChannelIsPinned:
    @pytest.mark.parametrize("second", ["127.0.0.1", "169.254.169.254"])
    async def test_send_connects_to_the_checked_address_with_original_sni(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sockets: _RecordingBackend,
        second: str,
    ) -> None:
        monkeypatch.setattr(_RESOLVER, _RebindingResolver(_PUBLIC, second))
        channel = WebhookChannel(
            ["https://hook.example.test/notify"], send_timeout_seconds=5.0
        )

        await channel.send(_message())

        assert sockets.connects == [(_PUBLIC, 443)]
        assert sockets.stream.sni == "hook.example.test"
        assert b"\r\nHost: hook.example.test\r\n" in sockets.stream.written

    async def test_send_to_a_denied_spelling_opens_no_socket(
        self, sockets: _RecordingBackend
    ) -> None:
        channel = WebhookChannel(["http://2130706433/hook"], send_timeout_seconds=5.0)
        with pytest.raises((ChannelSendError, SsrfError)):
            await channel.send(_message())
        assert sockets.connects == []

    async def test_env_proxy_is_not_used(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sockets: _RecordingBackend,
    ) -> None:
        """A proxy would receive the connection instead of the pinned address."""
        monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
        monkeypatch.setattr(_RESOLVER, _RebindingResolver(_PUBLIC))
        channel = WebhookChannel(
            ["https://hook.example.test/notify"], send_timeout_seconds=5.0
        )
        await channel.send(_message())
        assert sockets.connects == [(_PUBLIC, 443)]


class TestReportWebhookIsPinned:
    @pytest.mark.parametrize("second", ["127.0.0.1", "169.254.169.254"])
    async def test_report_webhook_connects_to_the_checked_address(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sockets: _RecordingBackend,
        second: str,
    ) -> None:
        monkeypatch.setattr(_RESOLVER, _RebindingResolver(_PUBLIC, second))
        monkeypatch.setattr(delivery, "_webhook_signing_secret", lambda _f: None)
        settings = Settings().model_copy(
            update={
                "reporting": ReportingSettings(
                    webhook_url="https://hook.example.test/report"
                )
            }
        )
        view = delivery.ReportView(
            id=1,
            key="r1",
            title="t",
            description="d",
            area="other",
            username="u",
            project_id=None,
            project_name=None,
            camera_id=None,
            reported_at="2026-10-28 12:00:00",
            created_at="2026-10-28 12:00:00",
            zip_name="r1.zip",
            zip_bytes=None,
            gitea_issue_number=None,
            gitea_issue_url=None,
            gitea_attachment_ok=False,
        )

        await delivery.send_webhook(MagicMock(), settings, view)

        assert sockets.connects == [(_PUBLIC, 443)]
        assert sockets.stream.sni == "hook.example.test"


class TestPostRenderWebhookIsPinned:
    """#148: the post-render ``external_trigger`` webhook is pinned too."""

    @pytest.mark.parametrize("second", ["127.0.0.1", "169.254.169.254"])
    async def test_trigger_connects_to_the_checked_address(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sockets: _RecordingBackend,
        second: str,
    ) -> None:
        monkeypatch.setattr(_RESOLVER, _RebindingResolver(_PUBLIC, second))

        await post_actions._webhook(
            Settings(),
            project_id=1,
            job_id=2,
            spec={"url": "https://hook.example.test/render"},
        )

        assert sockets.connects == [(_PUBLIC, 443)]
        assert sockets.stream.sni == "hook.example.test"
        assert b"\r\nHost: hook.example.test\r\n" in sockets.stream.written

    async def test_trigger_to_a_denied_spelling_opens_no_socket(
        self, sockets: _RecordingBackend
    ) -> None:
        with pytest.raises(SsrfError):
            await post_actions._webhook(
                Settings(), project_id=1, job_id=2, spec={"url": "http://2130706433/x"}
            )
        assert sockets.connects == []

    async def test_trigger_ignores_an_env_proxy(
        self, monkeypatch: pytest.MonkeyPatch, sockets: _RecordingBackend
    ) -> None:
        monkeypatch.setenv("HTTPS_PROXY", "http://10.9.9.9:3128")
        monkeypatch.setattr(_RESOLVER, _RebindingResolver(_PUBLIC))
        await post_actions._webhook(
            Settings(),
            project_id=1,
            job_id=2,
            spec={"url": "https://hook.example.test/render"},
        )
        assert sockets.connects == [(_PUBLIC, 443)]


def test_module_resolver_is_the_patched_seam() -> None:
    """The tests above patch ``ssrf.socket.getaddrinfo``; keep that seam real."""
    assert ssrf.socket is socket
