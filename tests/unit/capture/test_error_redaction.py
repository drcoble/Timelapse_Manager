"""A camera's login never reaches stored errors, events, status or notices (#69).

An error raised from a camera request can carry the URL it was fetching,
``user:pass@`` included (a legacy row, an ffmpeg stderr line, an httpx message).
The supervisor records it in ``CaptureState.last_error``, in event rows and in
logs; the status endpoints, the dashboard and the notification channels repeat
it. These tests put a recognisable secret in the error and look for it at every
record site and every output.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from tests.unit.test_campaign_bounds import (
    _INTERVAL,
    _make_settings,
    _permissive_disk_monitor,
)
from timelapse_manager.cameras.base import OtherCaptureError, TimeoutCaptureError
from timelapse_manager.capture.supervisor import (
    CaptureState,
    CaptureSupervisor,
    CaptureTarget,
)
from timelapse_manager.db.models import Event
from timelapse_manager.db.session import session_scope
from timelapse_manager.logging import redact_text
from timelapse_manager.monitoring.channels.base import NotificationMessage
from timelapse_manager.monitoring.channels.smtp import SMTPChannel
from timelapse_manager.monitoring.channels.webhook import WebhookChannel

_SECRET = "hunter2-camera-pw"
_LEAKY = f"cannot reach http://admin:{_SECRET}@10.0.16.5/snap.jpg"


def _supervisor(tmp_path: Path, factory: sessionmaker[Session]) -> CaptureSupervisor:
    return CaptureSupervisor(
        _make_settings(tmp_path), factory, disk_monitor=_permissive_disk_monitor()
    )


def _target() -> CaptureTarget:
    return CaptureTarget(
        project_id=1, project_name="p", camera_id=1, interval_seconds=_INTERVAL
    )


def _events(factory: sessionmaker[Session]) -> list[Event]:
    with session_scope(factory) as session:
        rows = list(session.execute(select(Event)).scalars())
        session.expunge_all()
        return rows


class TestRedactText:
    @pytest.mark.parametrize(
        "text",
        [
            f"rtsp://admin:{_SECRET}@host/x",
            f"rtsp://admin:p@{_SECRET}@host/x",  # an unescaped @ in the password
            f"https://admin:{_SECRET}@10.0.16.5:8443/snap?x=1",
            f"failed: http://{_SECRET}@host",
        ],
    )
    def test_masks_the_whole_userinfo(self, text: str) -> None:
        masked = redact_text(text)
        assert _SECRET not in masked
        assert "***@" in masked

    def test_keeps_the_host_for_diagnosis(self) -> None:
        assert redact_text("http://u:p@10.0.16.5/s") == "http://***@10.0.16.5/s"

    def test_plain_text_and_email_are_untouched(self) -> None:
        assert (
            redact_text("contact ops@example.com now") == "contact ops@example.com now"
        )
        assert redact_text("http://10.0.16.5/a@b") == "http://10.0.16.5/a@b"


class TestSupervisorRecordSites:
    async def test_transient_failure_stores_a_redacted_last_error(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        supervisor = _supervisor(tmp_path, migrated_factory)
        state = CaptureState(project_id=1, camera_id=1)
        supervisor._record_transient_failure(  # noqa: SLF001
            state, TimeoutCaptureError(_LEAKY), random.Random(0)
        )
        assert state.last_error is not None
        assert _SECRET not in state.last_error
        assert "10.0.16.5" in state.last_error
        await supervisor.stop()

    async def test_cycle_error_redacts_state_and_event(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        supervisor = _supervisor(tmp_path, migrated_factory)
        state = CaptureState(project_id=1, camera_id=1)
        await supervisor._contain_cycle_error(  # noqa: SLF001
            _target(), state, RuntimeError(_LEAKY), 60.0
        )
        assert state.last_error is not None and _SECRET not in state.last_error
        for event in _events(migrated_factory):
            assert _SECRET not in event.message
            assert _SECRET not in str(event.event_metadata)
        await supervisor.stop()

    async def test_write_event_is_a_redaction_chokepoint(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        supervisor = _supervisor(tmp_path, migrated_factory)
        supervisor._write_event(  # noqa: SLF001
            scope_id=1,
            level="warning",
            message=_LEAKY,
            metadata={
                "error": _LEAKY,
                "nested": [_LEAKY],
                "dedup_key": "1:noon",
                "n": 3,
            },
        )
        (event,) = _events(migrated_factory)
        assert _SECRET not in event.message
        assert _SECRET not in str(event.event_metadata)
        assert event.event_metadata is not None
        # Identifiers the capture path reads back are not masked by field name.
        assert event.event_metadata["n"] == 3
        assert event.event_metadata["dedup_key"] == "1:noon"
        await supervisor.stop()

    async def test_runner_problem_description_is_redacted(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        supervisor = _supervisor(tmp_path, migrated_factory)

        async def boom() -> None:
            raise OtherCaptureError(_LEAKY)

        task = asyncio.ensure_future(boom())
        await asyncio.wait([task])
        runner = SimpleNamespace(
            task=task, state=CaptureState(project_id=1, camera_id=1)
        )
        problem = supervisor._runner_problem(runner)  # type: ignore[arg-type]  # noqa: SLF001
        assert problem is not None and _SECRET not in problem
        await supervisor.stop()

    async def test_capture_failure_log_is_redacted(
        self,
        migrated_factory: sessionmaker[Session],
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        supervisor = _supervisor(tmp_path, migrated_factory)
        state = CaptureState(project_id=1, camera_id=1)

        async def fail(target: object, state: object) -> None:
            raise OtherCaptureError(_LEAKY)

        supervisor._capture_once = fail  # type: ignore[method-assign]  # noqa: SLF001
        with caplog.at_level(logging.WARNING):
            await supervisor._attempt_capture(  # noqa: SLF001
                _target(), state, random.Random(0)
            )
        assert "capture failed" in caplog.text
        assert _SECRET not in caplog.text
        assert state.last_error is not None and _SECRET not in state.last_error
        await supervisor.stop()


class TestNotificationChannels:
    def _message(self) -> NotificationMessage:
        return NotificationMessage(
            event_type="capture.failed",
            scope="project",
            scope_id=1,
            level="warning",
            message=_LEAKY,
            timestamp=datetime(2026, 10, 28, tzinfo=UTC),
            metadata={"error": _LEAKY, "token": _SECRET},
        )

    def test_webhook_payload_is_redacted(self) -> None:
        payload = WebhookChannel._payload(self._message())  # noqa: SLF001
        assert _SECRET not in str(payload)
        assert payload["metadata"]["error"].count("***@") == 1

    def test_email_body_is_redacted(self) -> None:
        body = SMTPChannel._body(self._message())  # noqa: SLF001
        assert _SECRET not in body
        assert "***@10.0.16.5" in body
