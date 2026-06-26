"""Unit tests for SMTPChannel and WebhookChannel.

SMTP: monkeypatched smtplib — sends, sets timeout, STARTTLS/SSL/login per
security mode, ChannelSendError on transport failure, password never logged.

Webhook: async context-manager stub — POSTs JSON payload, follow_redirects=False,
calls validate_outbound_url, ChannelSendError on 5xx/transport failure.
"""

from __future__ import annotations

import smtplib
from datetime import datetime
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

from timelapse_manager.monitoring.channels import ChannelSendError, NotificationMessage
from timelapse_manager.monitoring.channels.smtp import SMTPChannel, SMTPConfig
from timelapse_manager.monitoring.channels.webhook import WebhookChannel

# ---------------------------------------------------------------------------
# Test message fixture
# ---------------------------------------------------------------------------


def _msg(
    event_type: str = "capture.gap",
    level: str = "warning",
    scope: str = "system",
    scope_id: int | None = None,
    message: str = "A test notification",
    metadata: dict | None = None,
) -> NotificationMessage:
    return NotificationMessage(
        event_type=event_type,
        scope=scope,
        scope_id=scope_id,
        level=level,
        message=message,
        timestamp=datetime(2026, 1, 15, 12, 0, 0),
        metadata=metadata,
    )


def _smtp_config(
    security: str = "none",
    username: str | None = None,
    password: str | None = None,
) -> SMTPConfig:
    return SMTPConfig(
        server="mail.example.com",
        port=587,
        security=security,
        username=username,
        password=password,
        from_address="alerts@example.com",
        recipients=("ops@example.com",),
    )


# ---------------------------------------------------------------------------
# Async context manager stub for httpx.AsyncClient
# ---------------------------------------------------------------------------


class _AsyncClientStub:
    """An async context manager stub that replaces httpx.AsyncClient in tests.

    Pass a ``post_fn`` coroutine to intercept client.post() calls.
    """

    def __init__(
        self,
        post_fn=None,
        ctor_kwargs_sink: dict | None = None,
        **kwargs: Any,
    ) -> None:
        if ctor_kwargs_sink is not None:
            ctor_kwargs_sink.update(kwargs)
        self._post_fn = post_fn

    async def __aenter__(self) -> _AsyncClientStub:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def post(self, url: str, **kwargs: Any) -> Any:
        if self._post_fn is not None:
            return await self._post_fn(url, **kwargs)
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        return resp


# ---------------------------------------------------------------------------
# SMTP channel — monkeypatched smtplib
# ---------------------------------------------------------------------------


class TestSMTPChannelSend:
    def _mock_smtp(self) -> MagicMock:
        m = MagicMock()
        m.quit.return_value = None
        return m

    async def test_smtp_name_is_email(self) -> None:
        ch = SMTPChannel(_smtp_config(), send_timeout_seconds=5.0)
        assert ch.name == "email"

    async def test_send_calls_smtp_with_correct_server_and_port(self) -> None:
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP", return_value=mock_smtp) as smtp_cls:
            ch = SMTPChannel(_smtp_config(security="none"), send_timeout_seconds=5.0)
            await ch.send(_msg())
        smtp_cls.assert_called_once_with("mail.example.com", 587, timeout=5.0)

    async def test_send_passes_timeout_to_smtp(self) -> None:
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP", return_value=mock_smtp) as smtp_cls:
            ch = SMTPChannel(_smtp_config(security="none"), send_timeout_seconds=12.5)
            await ch.send(_msg())
        _, kwargs = smtp_cls.call_args
        assert kwargs["timeout"] == 12.5

    async def test_starttls_mode_calls_starttls(self) -> None:
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            ch = SMTPChannel(
                _smtp_config(security="starttls"), send_timeout_seconds=5.0
            )
            await ch.send(_msg())
        mock_smtp.starttls.assert_called_once()

    async def test_none_mode_does_not_call_starttls(self) -> None:
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            ch = SMTPChannel(_smtp_config(security="none"), send_timeout_seconds=5.0)
            await ch.send(_msg())
        mock_smtp.starttls.assert_not_called()

    async def test_tls_mode_uses_smtp_ssl(self) -> None:
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP_SSL", return_value=mock_smtp) as ssl_cls:
            ch = SMTPChannel(_smtp_config(security="tls"), send_timeout_seconds=5.0)
            await ch.send(_msg())
        ssl_cls.assert_called_once()

    async def test_login_called_when_credentials_provided(self) -> None:
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            ch = SMTPChannel(
                _smtp_config(security="none", username="alice", password="s3cr3t"),
                send_timeout_seconds=5.0,
            )
            await ch.send(_msg())
        mock_smtp.login.assert_called_once_with("alice", "s3cr3t")

    async def test_login_not_called_when_no_credentials(self) -> None:
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            ch = SMTPChannel(
                _smtp_config(security="none", username=None, password=None),
                send_timeout_seconds=5.0,
            )
            await ch.send(_msg())
        mock_smtp.login.assert_not_called()

    async def test_send_message_is_called(self) -> None:
        mock_smtp = self._mock_smtp()
        with patch("smtplib.SMTP", return_value=mock_smtp):
            ch = SMTPChannel(_smtp_config(security="none"), send_timeout_seconds=5.0)
            await ch.send(_msg())
        mock_smtp.send_message.assert_called_once()


class TestSMTPChannelErrors:
    async def test_oserror_raises_channel_send_error(self) -> None:
        with patch("smtplib.SMTP", side_effect=OSError("connection refused")):
            ch = SMTPChannel(_smtp_config(), send_timeout_seconds=5.0)
            with pytest.raises(ChannelSendError):
                await ch.send(_msg())

    async def test_smtp_exception_raises_channel_send_error(self) -> None:
        with patch("smtplib.SMTP", side_effect=smtplib.SMTPException("auth failed")):
            ch = SMTPChannel(_smtp_config(), send_timeout_seconds=5.0)
            with pytest.raises(ChannelSendError):
                await ch.send(_msg())

    async def test_channel_send_error_message_does_not_contain_password(
        self,
    ) -> None:
        """The ChannelSendError message must never expose the SMTP password."""
        with patch(
            "smtplib.SMTP", side_effect=smtplib.SMTPAuthenticationError(535, "auth")
        ):
            ch = SMTPChannel(
                _smtp_config(username="user", password="s3cr3t"),
                send_timeout_seconds=5.0,
            )
            with pytest.raises(ChannelSendError) as exc_info:
                await ch.send(_msg())
        assert "s3cr3t" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# SMTP — password never appears in log records during send
# ---------------------------------------------------------------------------


class TestSMTPPasswordNotLogged:
    async def test_password_not_in_any_log_record_during_send(self, caplog) -> None:
        """No log record emitted during send() may contain the SMTP password.

        This forces login() to raise so the SMTP module's error-handling path
        runs (a success-path test emits no log records and would prove nothing).
        The ChannelSendError must not embed the password, and no logger at DEBUG
        or above may emit it either.
        """
        import logging

        password = "ultra-s3cr3t-pw"
        mock_smtp = MagicMock()
        mock_smtp.quit.return_value = None
        mock_smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, "bad auth")

        ch = SMTPChannel(
            _smtp_config(username="user", password=password),
            send_timeout_seconds=5.0,
        )
        with (
            patch("smtplib.SMTP", return_value=mock_smtp),
            caplog.at_level(logging.DEBUG, logger="timelapse_manager"),
            pytest.raises(ChannelSendError),
        ):
            await ch.send(_msg())

        for record in caplog.records:
            assert password not in record.getMessage(), (
                f"Password found in log record: {record.getMessage()!r}"
            )


# ---------------------------------------------------------------------------
# Webhook channel — _AsyncClientStub approach
# ---------------------------------------------------------------------------


class TestWebhookChannelSend:
    async def test_webhook_name_is_webhook(self) -> None:
        ch = WebhookChannel(
            ["https://hooks.example.com/notify"], send_timeout_seconds=5.0
        )
        assert ch.name == "webhook"

    async def test_webhook_posts_to_correct_url(self) -> None:
        posted_urls: list[str] = []

        async def recording_post(url: str, **kwargs: Any) -> MagicMock:
            posted_urls.append(url)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        def make_client(**kwargs: Any) -> _AsyncClientStub:
            return _AsyncClientStub(post_fn=recording_post)

        with (
            patch("httpx.AsyncClient", side_effect=make_client),
            patch(
                "timelapse_manager.monitoring.channels.webhook.validate_outbound_url",
                side_effect=lambda u: u,
            ),
        ):
            ch = WebhookChannel(
                ["https://hooks.example.com/notify"], send_timeout_seconds=5.0
            )
            await ch.send(_msg(message="disk low"))

        assert len(posted_urls) == 1
        assert "hooks.example.com" in posted_urls[0]

    async def test_webhook_payload_shape(self) -> None:
        """JSON body must carry event_type, level, scope, scope_id, message, ts."""
        captured_payload: dict = {}

        async def recording_post(url: str, **kwargs: Any) -> MagicMock:
            payload = kwargs.get("json", {})
            captured_payload.update(payload)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        def make_client(**kwargs: Any) -> _AsyncClientStub:
            return _AsyncClientStub(post_fn=recording_post)

        with (
            patch("httpx.AsyncClient", side_effect=make_client),
            patch(
                "timelapse_manager.monitoring.channels.webhook.validate_outbound_url",
                side_effect=lambda u: u,
            ),
        ):
            ch = WebhookChannel(["https://h.example.com"], send_timeout_seconds=5.0)
            await ch.send(
                _msg(
                    event_type="render.failed",
                    level="error",
                    scope="project",
                    scope_id=3,
                    message="render job failed",
                )
            )

        for key in ("event_type", "level", "scope", "scope_id", "message", "timestamp"):
            assert key in captured_payload, f"Missing key {key!r} in webhook payload"
        assert captured_payload["event_type"] == "render.failed"
        assert captured_payload["scope_id"] == 3

    async def test_webhook_calls_validate_outbound_url(self) -> None:
        """validate_outbound_url is called for every target URL (SSRF seam)."""
        validated: list[str] = []

        async def recording_post(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        def make_client(**kwargs: Any) -> _AsyncClientStub:
            return _AsyncClientStub(post_fn=recording_post)

        def recording_validate(url: str) -> str:
            validated.append(url)
            return url

        with (
            patch("httpx.AsyncClient", side_effect=make_client),
            patch(
                "timelapse_manager.monitoring.channels.webhook.validate_outbound_url",
                side_effect=recording_validate,
            ),
        ):
            ch = WebhookChannel(
                ["https://h.example.com/a", "https://h.example.com/b"],
                send_timeout_seconds=5.0,
            )
            await ch.send(_msg())

        assert len(validated) == 2

    async def test_webhook_follow_redirects_false(self) -> None:
        """follow_redirects must be False — the constructor kwarg is verified."""
        ctor_kwargs: dict = {}

        async def noop_post(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        def capturing_client(**kwargs: Any) -> _AsyncClientStub:
            ctor_kwargs.update(kwargs)
            return _AsyncClientStub(post_fn=noop_post)

        with (
            patch("httpx.AsyncClient", side_effect=capturing_client),
            patch(
                "timelapse_manager.monitoring.channels.webhook.validate_outbound_url",
                side_effect=lambda u: u,
            ),
        ):
            ch = WebhookChannel(["https://h.example.com"], send_timeout_seconds=5.0)
            await ch.send(_msg())

        assert ctor_kwargs.get("follow_redirects") is False
        # Explicit timeout is always set — a hung request must not wedge shutdown.
        assert ctor_kwargs.get("timeout") == 5.0

    async def test_webhook_5xx_raises_channel_send_error(self) -> None:
        """A 5xx response must surface as ChannelSendError."""

        async def failing_post(url: str, **kwargs: Any) -> MagicMock:
            request = httpx.Request("POST", url)
            raise httpx.HTTPStatusError(
                "500 Internal Server Error",
                request=request,
                response=httpx.Response(500, request=request),
            )

        def make_client(**kwargs: Any) -> _AsyncClientStub:
            return _AsyncClientStub(post_fn=failing_post)

        with (
            patch("httpx.AsyncClient", side_effect=make_client),
            patch(
                "timelapse_manager.monitoring.channels.webhook.validate_outbound_url",
                side_effect=lambda u: u,
            ),
        ):
            ch = WebhookChannel(["https://h.example.com"], send_timeout_seconds=5.0)
            with pytest.raises(ChannelSendError):
                await ch.send(_msg())

    async def test_webhook_transport_error_raises_channel_send_error(self) -> None:
        """A transport-level httpx error surfaces as ChannelSendError."""

        async def failing_post(url: str, **kwargs: Any) -> MagicMock:
            raise httpx.ConnectError("connection refused")

        def make_client(**kwargs: Any) -> _AsyncClientStub:
            return _AsyncClientStub(post_fn=failing_post)

        with (
            patch("httpx.AsyncClient", side_effect=make_client),
            patch(
                "timelapse_manager.monitoring.channels.webhook.validate_outbound_url",
                side_effect=lambda u: u,
            ),
        ):
            ch = WebhookChannel(["https://h.example.com"], send_timeout_seconds=5.0)
            with pytest.raises(ChannelSendError):
                await ch.send(_msg())
