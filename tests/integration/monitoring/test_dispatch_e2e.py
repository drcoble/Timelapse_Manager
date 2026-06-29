"""End-to-end integration: real SMTPChannel + WebhookChannel wired to the dispatcher.

Uses monkeypatched transports (smtplib / httpx) so no real network is touched.
Verifies that a multi-channel routed event reaches both channels through run_once
and that redaction applies end-to-end.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from timelapse_manager.config.settings import MonitoringSettings
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import (
    EventType,
    NotificationDispatcher,
    log_event,
)
from timelapse_manager.monitoring.channels.smtp import SMTPChannel, SMTPConfig
from timelapse_manager.monitoring.channels.webhook import WebhookChannel

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_settings(**overrides: Any) -> MonitoringSettings:
    defaults: dict = {
        "autostart": False,
        "poll_interval_seconds": 0.1,
        "max_retries": 1,
        "retry_backoff_seconds": 0.0,
        "debounce_window_seconds": 0.0,
        "channel_send_timeout_seconds": 10.0,
    }
    defaults.update(overrides)
    return MonitoringSettings(**defaults)


def _smtp_config() -> SMTPConfig:
    return SMTPConfig(
        server="mail.example.com",
        port=587,
        security="none",
        username=None,
        password=None,
        from_address="alerts@example.com",
        recipients=("ops@example.com",),
    )


_MULTI_CHANNEL_RULE = [
    {
        "event_types": ["all"],
        "min_level": "info",
        "channels": ["email", "webhook"],
    }
]


class _AsyncClientStub:
    """Minimal async context manager stub for httpx.AsyncClient."""

    def __init__(self, post_fn=None, **kwargs: Any) -> None:
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
# End-to-end: both channels deliver
# ---------------------------------------------------------------------------


class TestDispatchE2EBothChannels:
    async def test_run_once_delivers_to_smtp_channel(self, migrated_factory) -> None:
        """A routed event reaches the SMTP channel via run_once."""
        sent_emails: list = []

        mock_smtp = MagicMock()
        mock_smtp.quit.return_value = None
        mock_smtp.send_message.side_effect = lambda em: sent_emails.append(em)

        async def noop_post(url: str, **kwargs: Any) -> MagicMock:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        def make_webhook_client(**kwargs: Any) -> _AsyncClientStub:
            return _AsyncClientStub(post_fn=noop_post)

        smtp_ch = SMTPChannel(_smtp_config(), send_timeout_seconds=5.0)
        webhook_ch = WebhookChannel(
            ["https://hooks.example.com/notify"], send_timeout_seconds=5.0
        )

        disp = NotificationDispatcher(
            session_factory=migrated_factory,
            channels=[smtp_ch, webhook_ch],
            settings=_minimal_settings(),
            routing_rules_fn=lambda: _MULTI_CHANNEL_RULE,
        )

        with session_scope(migrated_factory) as session:
            log_event(
                session,
                scope="system",
                scope_id=None,
                level="warning",
                message="disk space is running low",
                type=EventType.STORAGE_DISK_LOW.value,
            )

        with (
            patch("smtplib.SMTP", return_value=mock_smtp),
            patch("httpx.AsyncClient", side_effect=make_webhook_client),
            patch(
                "timelapse_manager.monitoring.channels.webhook.validate_outbound_url",
                side_effect=lambda u: u,
            ),
        ):
            count = await disp.run_once()

        assert count == 1
        assert len(sent_emails) == 1

    async def test_run_once_delivers_to_webhook_channel(self, migrated_factory) -> None:
        """A routed event reaches the webhook channel via run_once."""
        posted_payloads: list[dict] = []

        async def recording_post(url: str, **kwargs: Any) -> MagicMock:
            payload = kwargs.get("json") or {}
            posted_payloads.append(payload)
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        def make_webhook_client(**kwargs: Any) -> _AsyncClientStub:
            return _AsyncClientStub(post_fn=recording_post)

        mock_smtp = MagicMock()
        mock_smtp.quit.return_value = None

        smtp_ch = SMTPChannel(_smtp_config(), send_timeout_seconds=5.0)
        webhook_ch = WebhookChannel(
            ["https://hooks.example.com/notify"], send_timeout_seconds=5.0
        )

        disp = NotificationDispatcher(
            session_factory=migrated_factory,
            channels=[smtp_ch, webhook_ch],
            settings=_minimal_settings(),
            routing_rules_fn=lambda: _MULTI_CHANNEL_RULE,
        )

        with session_scope(migrated_factory) as session:
            log_event(
                session,
                scope="project",
                scope_id=1,
                level="error",
                message="render job failed unexpectedly",
                type=EventType.RENDER_FAILED.value,
            )

        with (
            patch("smtplib.SMTP", return_value=mock_smtp),
            patch("httpx.AsyncClient", side_effect=make_webhook_client),
            patch(
                "timelapse_manager.monitoring.channels.webhook.validate_outbound_url",
                side_effect=lambda u: u,
            ),
        ):
            await disp.run_once()

        assert len(posted_payloads) == 1
        payload = posted_payloads[0]
        assert payload["event_type"] == EventType.RENDER_FAILED.value
        assert payload["scope"] == "project"
        assert payload["scope_id"] == 1

    async def test_run_once_both_channels_receive_same_event(
        self, migrated_factory
    ) -> None:
        """A multi-channel rule causes both SMTP and webhook to be called."""
        smtp_calls: list = []
        webhook_calls: list[dict] = []

        mock_smtp = MagicMock()
        mock_smtp.quit.return_value = None
        mock_smtp.send_message.side_effect = lambda e: smtp_calls.append(e)

        async def recording_post(url: str, **kwargs: Any) -> MagicMock:
            webhook_calls.append(kwargs.get("json", {}))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        def make_webhook_client(**kwargs: Any) -> _AsyncClientStub:
            return _AsyncClientStub(post_fn=recording_post)

        smtp_ch = SMTPChannel(_smtp_config(), send_timeout_seconds=5.0)
        webhook_ch = WebhookChannel(
            ["https://hooks.example.com/notify"], send_timeout_seconds=5.0
        )

        disp = NotificationDispatcher(
            session_factory=migrated_factory,
            channels=[smtp_ch, webhook_ch],
            settings=_minimal_settings(),
            routing_rules_fn=lambda: _MULTI_CHANNEL_RULE,
        )

        with session_scope(migrated_factory) as session:
            log_event(
                session,
                scope="camera",
                scope_id=2,
                level="error",
                message="camera went offline",
                type=EventType.CAMERA_OFFLINE_THRESHOLD.value,
            )

        with (
            patch("smtplib.SMTP", return_value=mock_smtp),
            patch("httpx.AsyncClient", side_effect=make_webhook_client),
            patch(
                "timelapse_manager.monitoring.channels.webhook.validate_outbound_url",
                side_effect=lambda u: u,
            ),
        ):
            await disp.run_once()

        assert len(smtp_calls) == 1, "SMTP channel was not called"
        assert len(webhook_calls) == 1, "Webhook channel was not called"


# ---------------------------------------------------------------------------
# End-to-end: redaction
# ---------------------------------------------------------------------------


class TestDispatchE2ERedaction:
    async def test_webhook_payload_does_not_contain_stored_credential(
        self, migrated_factory
    ) -> None:
        """Credentials in the event message are redacted before dispatch."""
        posted_payloads: list[dict] = []

        async def recording_post(url: str, **kwargs: Any) -> MagicMock:
            posted_payloads.append(kwargs.get("json", {}))
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            return resp

        def make_webhook_client(**kwargs: Any) -> _AsyncClientStub:
            return _AsyncClientStub(post_fn=recording_post)

        webhook_ch = WebhookChannel(
            ["https://hooks.example.com/notify"], send_timeout_seconds=5.0
        )

        rules = [{"event_types": ["all"], "min_level": "info", "channels": ["webhook"]}]
        disp = NotificationDispatcher(
            session_factory=migrated_factory,
            channels=[webhook_ch],
            settings=_minimal_settings(),
            routing_rules_fn=lambda: rules,
        )

        # The message contains a URL with credentials; log_event redacts it.
        raw_msg = "rtsp://admin:s3cr3t@192.0.2.10/stream connection lost"
        with session_scope(migrated_factory) as session:
            log_event(
                session,
                scope="system",
                scope_id=None,
                level="error",
                message=raw_msg,
                type=EventType.CAMERA_OFFLINE_THRESHOLD.value,
            )

        with (
            patch("httpx.AsyncClient", side_effect=make_webhook_client),
            patch(
                "timelapse_manager.monitoring.channels.webhook.validate_outbound_url",
                side_effect=lambda u: u,
            ),
        ):
            await disp.run_once()

        assert len(posted_payloads) == 1
        payload_text = str(posted_payloads[0])
        assert "s3cr3t" not in payload_text, (
            "Credential found in webhook payload — redaction failed"
        )
