"""The notification channel contract and the message it carries.

A concrete channel (email, webhook, ...) implements :class:`NotificationChannel`
and is injected into the dispatcher. The dispatcher treats every channel
uniformly: it builds a :class:`NotificationMessage` from a logged event and calls
:meth:`NotificationChannel.send`, wrapping the call in an outer
``asyncio.wait_for`` timeout so a hanging channel cannot block a clean shutdown.

The timeout contract is load-bearing and split across two layers:

* The **dispatcher** wraps each ``send`` in an outer ``asyncio.wait_for`` so a
  channel that cooperatively awaits can be timed out and cancelled.
* The **channel** MUST additionally set its own socket/HTTP-level timeout on any
  blocking transport it uses. A blocking synchronous send (for example
  ``smtplib`` driven inside a worker thread) cannot be interrupted by cancelling
  the awaiting task -- the thread keeps running until the socket operation
  returns or its own timeout fires. Only the channel's internal timeout bounds
  that case, so it is mandatory, not advisory.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from datetime import datetime
from typing import Any


class ChannelSendError(Exception):
    """Raised by a channel when a delivery attempt fails at the transport layer.

    Channels signal a *recoverable* delivery failure (a refused connection, a
    timeout, a 5xx response) by raising this. The dispatcher catches it, applies
    bounded retry with backoff, and -- only after retries are exhausted -- records
    an in-application delivery-failure event. A channel must never let a raw
    transport exception escape ``send``; it either succeeds, raises this typed
    error, or returns normally.
    """


@dataclass(frozen=True)
class NotificationMessage:
    """An immutable, channel-agnostic description of an alert to deliver.

    Built by the dispatcher from a logged event row. Channels render it into
    their own format (an email body, a JSON webhook payload, ...). It carries no
    secrets and is safe to log after redaction.

    :param event_type: the dotted event-type identifier (for example
        ``"camera.offline_threshold"``); empty when the source row carried none.
    :param scope: the event scope -- ``"system"``, ``"camera"``, or ``"project"``.
    :param scope_id: the id of the scoped entity, or ``None`` for system scope.
    :param level: the severity name (``"info"``/``"warning"``/``"error"``/
        ``"critical"``).
    :param message: the human-readable event message.
    :param timestamp: when the event occurred (naive UTC, matching storage).
    :param metadata: the event's free-form details, or ``None``.
    """

    event_type: str
    scope: str
    scope_id: int | None
    level: str
    message: str
    timestamp: datetime
    metadata: dict[str, Any] | None


class NotificationChannel(abc.ABC):
    """Abstract base for an outbound notification delivery mechanism.

    Implementations are injected into the dispatcher; this package never imports
    a concrete channel. Each channel exposes a stable :attr:`name` (matched
    against routing rules) and an async :meth:`send`.

    **Timeout contract (mandatory).** ``send`` MUST set an explicit socket/HTTP
    timeout on whatever transport it uses. The dispatcher additionally wraps each
    ``send`` in an outer ``asyncio.wait_for`` so a cooperatively-awaiting channel
    can be cancelled on shutdown -- but a blocking synchronous transport (such as
    ``smtplib`` run in a thread) is *not* interruptible by that cancellation and
    is bounded only by the channel's own timeout. Omitting an internal timeout
    can therefore wedge shutdown; it is part of the contract.

    **Failure contract.** ``send`` must not let a raw transport exception escape.
    On a recoverable transport failure it raises :class:`ChannelSendError`, which
    the dispatcher catches and retries with backoff. Returning normally signals
    success.
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """The channel's stable identifier, as referenced by routing rules."""

    @abc.abstractmethod
    async def send(self, message: NotificationMessage) -> None:
        """Deliver ``message`` through this channel.

        :param message: the alert to deliver.
        :raises ChannelSendError: on a recoverable transport failure (the
            dispatcher retries with backoff, then records a delivery-failure
            event without re-notifying).
        """
