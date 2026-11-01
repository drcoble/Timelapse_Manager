"""The notification dispatcher's database work runs off the event loop (#62).

Driven through its real poll loop (``start``), as in production, so the loop
guard sees the dispatch tasks as background work. Two reads and a write used to
run on the loop: the routing rules (read from the notification settings row
each poll) and the delivery-failure event recorded after a channel's retries
are exhausted. Under a held SQLite write lock that write froze every request for
up to the busy timeout.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from sqlalchemy.orm import sessionmaker

from tests import loop_guard_support as guard
from timelapse_manager.config.settings import MonitoringSettings
from timelapse_manager.db.models import Event, NotificationSettings
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import (
    EventType,
    NotificationChannel,
    NotificationDispatcher,
    NotificationMessage,
    log_event,
)
from timelapse_manager.monitoring.channels.base import ChannelSendError


class _FailingChannel(NotificationChannel):
    """A channel whose every send fails at the transport."""

    def __init__(self) -> None:
        self.attempts = 0

    @property
    def name(self) -> str:
        return "webhook"

    async def send(self, message: NotificationMessage) -> None:
        self.attempts += 1
        raise ChannelSendError("connection refused")


def _store_routing_rules(factory: sessionmaker, rules: list[dict[str, Any]]) -> None:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        row = session.get(NotificationSettings, 1)
        if row is None:
            row = NotificationSettings(id=1)
            session.add(row)
        row.routing_rules = rules


def _delivery_failures(factory: sessionmaker) -> list[str]:  # type: ignore[type-arg]
    with session_scope(factory) as session:
        rows = session.query(Event).order_by(Event.id).all()
        return [
            row.message
            for row in rows
            if (row.event_metadata or {}).get("type")
            == EventType.NOTIFY_DELIVERY_FAILED.value
        ]


async def test_failed_delivery_is_recorded_without_loop_db_work(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
) -> None:
    _store_routing_rules(
        migrated_factory,
        [{"event_types": ["all"], "min_level": "info", "channels": ["webhook"]}],
    )
    channel = _FailingChannel()
    dispatcher = NotificationDispatcher(
        migrated_factory,
        channels=[channel],
        settings=MonitoringSettings(
            autostart=False,
            poll_interval_seconds=0.1,
            max_retries=2,
            retry_backoff_seconds=0.0,
            debounce_window_seconds=0.0,
        ),
    )
    await dispatcher.start()
    try:
        with session_scope(migrated_factory) as session:
            log_event(
                session,
                scope="system",
                scope_id=None,
                level="error",
                type=EventType.RENDER_FAILED.value,
                message="render 7 failed: test",
            )
        deadline = time.monotonic() + 10.0
        while not _delivery_failures(migrated_factory):
            if time.monotonic() > deadline:
                pytest.fail("no delivery-failure event was recorded")
            await asyncio.sleep(0.05)
    finally:
        await dispatcher.stop()

    assert channel.attempts == 2
    assert len(_delivery_failures(migrated_factory)) == 1
    background = [v for v in guard.collect() if v.background]
    assert background == [], guard.failure_message(background)


async def test_cursor_and_channel_rebuild_run_off_the_loop(
    migrated_factory: sessionmaker,  # type: ignore[type-arg]
) -> None:
    """WP-67-R: the start-position read, the cursor writes and the channel
    rebuild (a settings read plus secret decryption) all run in worker threads.
    """
    from timelapse_manager.db.models import NotificationCursor
    from timelapse_manager.monitoring.channels import StoredChannelsProvider

    _store_routing_rules(
        migrated_factory,
        [{"event_types": ["all"], "min_level": "info", "channels": ["webhook"]}],
    )
    dispatcher = NotificationDispatcher(
        migrated_factory,
        channels=[],
        settings=MonitoringSettings(autostart=False, poll_interval_seconds=0.05),
        channels_provider=StoredChannelsProvider(send_timeout_seconds=1.0),
    )
    await dispatcher.start()
    try:
        with session_scope(migrated_factory) as session:
            row = session.get(NotificationSettings, 1)
            assert row is not None
            row.enabled_channels = ["webhook"]
            row.webhook_urls = ["https://hooks.example.test/notify"]
            log_event(
                session,
                scope="system",
                scope_id=None,
                level="info",
                message="cursor moves",
            )
        deadline = time.monotonic() + 10.0
        while "webhook" not in dispatcher._channels:  # noqa: SLF001
            if time.monotonic() > deadline:
                pytest.fail("the webhook channel was never rebuilt")
            await asyncio.sleep(0.05)
    finally:
        await dispatcher.stop()

    with session_scope(migrated_factory) as session:
        cursor = session.get(NotificationCursor, 1)
        assert cursor is not None and cursor.last_read_event_id >= 1
    background = [v for v in guard.collect() if v.background]
    assert background == [], guard.failure_message(background)
