"""HTTP webhook notification channel.

Delivers a notification as a small JSON ``POST`` to one or more configured URLs,
so an external system learns that an event fired. The channel is built once at
startup from the stored notification settings, and rebuilt by the dispatcher
when those settings change, so a URL list edited in the UI applies without a
restart.

Three safety properties mirror the post-render webhook:

* every target URL passes through the single outbound-URL validation seam (its
  async form, which resolves off the event loop), and the request then
  connects only to the address that seam validated (a pinned transport, so a
  DNS-rebinding answer cannot redirect the socket),
* redirects are never followed, and
* an explicit timeout is always set on the request, satisfying the channel
  contract (a cooperatively-awaiting ``httpx`` request is also cancellable by the
  dispatcher's outer ``asyncio.timeout``).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any

import httpx

from ...logging import redact, redact_text
from ...render.post_actions import validate_outbound_url_async
from ...security.pinned_http import pinned_async_transport
from .base import ChannelSendError, NotificationChannel, NotificationMessage

logger = logging.getLogger(__name__)

# The stable channel identifier matched against routing rules.
_CHANNEL_NAME = "webhook"

# Header carrying the HMAC-SHA256 signature of the request body, when a signing
# secret is configured for the channel. The value is ``sha256=<hex digest>``,
# matching the de-facto convention used by common webhook providers.
_SIGNATURE_HEADER = "X-Signature-256"


def sign_body(secret: str, body: bytes) -> str:
    """Return the ``X-Signature-256`` header value for ``body``.

    ``sha256=<hex HMAC-SHA256 of the exact body bytes>``, keyed with the
    webhook signing secret. Shared with the problem-report webhook so both
    surfaces sign identically.
    """
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


# Exported for the problem-report webhook, which signs the same way.
SIGNATURE_HEADER = _SIGNATURE_HEADER


class WebhookChannel(NotificationChannel):
    """POSTs a JSON notification to each configured webhook URL.

    A failure delivering to *any* configured URL is treated as a recoverable
    failure of the whole channel (the dispatcher retries with backoff): on the
    first failing target the channel raises :class:`ChannelSendError`. No
    credential embedded in a target URL is ever logged -- only the redacted form
    appears in diagnostics.
    """

    def __init__(
        self,
        urls: list[str],
        *,
        send_timeout_seconds: float,
        signing_secret: str | None = None,
    ) -> None:
        """Create the channel from the configured target URLs.

        :param urls: the webhook endpoints to notify.
        :param send_timeout_seconds: the per-request HTTP timeout. Required by
            the channel contract so a hung request cannot wedge shutdown.
        :param signing_secret: optional per-channel secret. When set, each POST
            carries an ``X-Signature-256`` header holding the HMAC-SHA256 digest
            of the exact request body, so the receiver can authenticate the
            payload. When ``None`` the request is sent unsigned and unchanged.
        """
        self._urls = [u for u in urls if u]
        self._timeout = max(0.1, send_timeout_seconds)
        self._signing_secret = signing_secret or None

    @property
    def name(self) -> str:
        """Return the stable channel identifier used by routing rules."""
        return _CHANNEL_NAME

    async def send(self, message: NotificationMessage) -> None:
        """POST ``message`` as JSON to every configured URL.

        :param message: the alert to deliver.
        :raises ChannelSendError: on a transport failure or a non-2xx response
            from any target (the dispatcher retries with backoff).
        """
        payload = self._payload(message)
        # A short-lived client per send: the channel ABC has no close hook and
        # the dispatcher never closes channels, so a long-lived client would
        # leak. Redirects are not followed; a timeout is always set. The pinned
        # transport connects only to the address the SSRF guard validated, so a
        # rebinding DNS answer cannot redirect the socket (no env proxy either).
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            transport=pinned_async_transport(),
            trust_env=False,
        ) as client:
            for url in self._urls:
                await self._post_one(client, url, payload)

    async def _post_one(
        self, client: httpx.AsyncClient, url: str, payload: dict[str, Any]
    ) -> None:
        """POST to a single validated URL, raising on transport or HTTP failure.

        When a signing secret is configured the body is serialized once and sent
        verbatim via ``content=`` so the signed bytes are exactly the bytes on the
        wire; ``httpx`` does not re-encode them. Without a secret the request is
        sent via ``json=`` exactly as before, leaving the unsigned path
        byte-for-byte unchanged.
        """
        # Resolved in a worker thread, never on the loop (#67 P6).
        target = await validate_outbound_url_async(url)
        try:
            if self._signing_secret is None:
                response = await client.post(target, json=payload)
            else:
                body = json.dumps(payload).encode("utf-8")
                response = await client.post(
                    target,
                    content=body,
                    headers={
                        "Content-Type": "application/json",
                        _SIGNATURE_HEADER: sign_body(self._signing_secret, body),
                    },
                )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise ChannelSendError(
                f"webhook returned HTTP {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            # Class name only -- the str() of an httpx error can embed the full
            # request URL (and thus any userinfo credential).
            raise ChannelSendError(
                f"webhook delivery failed: {exc.__class__.__name__}"
            ) from exc

    @staticmethod
    def _payload(message: NotificationMessage) -> dict[str, Any]:
        """Build the JSON body for a notification POST.

        The message and metadata are normally redacted at the source
        (``log_event`` scrubs them before persisting). They are redacted again
        here, as the last step before they leave the machine: an event row
        written by another path must not carry a camera URL's login to a third
        party (#69 CAM-S2, #67-S14).
        """
        return {
            "event_type": message.event_type,
            "level": message.level,
            "scope": message.scope,
            "scope_id": message.scope_id,
            "message": redact_text(message.message),
            "timestamp": message.timestamp.isoformat(),
            "metadata": redact(message.metadata),
        }
