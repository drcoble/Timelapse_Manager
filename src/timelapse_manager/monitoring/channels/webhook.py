"""HTTP webhook notification channel.

Delivers a notification as a small JSON ``POST`` to one or more configured URLs,
so an external system learns that an event fired. The channel is built once at
startup from the stored notification settings; changing the URL list in the UI
takes effect on the next restart (channel transport configuration is not
hot-reloaded -- only the routing rules are re-read per poll cycle).

Three safety properties mirror the post-render webhook:

* every target URL passes through the single outbound-URL validation seam so a
  later phase can enforce an SSRF deny-list in one place,
* redirects are never followed, and
* an explicit timeout is always set on the request, satisfying the channel
  contract (a cooperatively-awaiting ``httpx`` request is also cancellable by the
  dispatcher's outer ``wait_for``).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ...render.post_actions import validate_outbound_url
from .base import ChannelSendError, NotificationChannel, NotificationMessage

logger = logging.getLogger(__name__)

# The stable channel identifier matched against routing rules.
_CHANNEL_NAME = "webhook"


class WebhookChannel(NotificationChannel):
    """POSTs a JSON notification to each configured webhook URL.

    A failure delivering to *any* configured URL is treated as a recoverable
    failure of the whole channel (the dispatcher retries with backoff): on the
    first failing target the channel raises :class:`ChannelSendError`. No
    credential embedded in a target URL is ever logged -- only the redacted form
    appears in diagnostics.
    """

    def __init__(self, urls: list[str], *, send_timeout_seconds: float) -> None:
        """Create the channel from the configured target URLs.

        :param urls: the webhook endpoints to notify.
        :param send_timeout_seconds: the per-request HTTP timeout. Required by
            the channel contract so a hung request cannot wedge shutdown.
        """
        self._urls = [u for u in urls if u]
        self._timeout = max(0.1, send_timeout_seconds)

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
        # leak. Redirects are not followed; a timeout is always set.
        async with httpx.AsyncClient(
            timeout=self._timeout, follow_redirects=False
        ) as client:
            for url in self._urls:
                await self._post_one(client, url, payload)

    async def _post_one(
        self, client: httpx.AsyncClient, url: str, payload: dict[str, Any]
    ) -> None:
        """POST to a single validated URL, raising on transport or HTTP failure."""
        target = validate_outbound_url(url)
        try:
            response = await client.post(target, json=payload)
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

        The message and metadata are already redacted at the source
        (``log_event`` scrubs them before persisting), so they are safe to send.
        """
        return {
            "event_type": message.event_type,
            "level": message.level,
            "scope": message.scope,
            "scope_id": message.scope_id,
            "message": message.message,
            "timestamp": message.timestamp.isoformat(),
            "metadata": message.metadata,
        }
