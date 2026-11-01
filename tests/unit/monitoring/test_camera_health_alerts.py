"""Camera-scoped offline alerts in monitoring (#69, CAM-R6).

* The supervisor's one-time upgrade sweep clears the old project-scoped
  ``camera_offline`` alerts (which a camera-scoped recovery can never resolve)
  and seeds camera health from the camera-scoped ones.
* A camera-scoped ``camera_recovered`` auto-clears the camera-scoped alert
  through the dispatcher poll, and only that camera's.
* Notification routing does not look at scope: the camera-scoped alert reaches
  the channels a project-scoped one did.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from timelapse_manager.capture.camera_health import HealthTransition
from timelapse_manager.capture.supervisor import CaptureSupervisor
from timelapse_manager.config.settings import (
    CaptureSettings,
    DatabaseSettings,
    LoggingSettings,
    MonitoringSettings,
    PathsSettings,
    Settings,
)
from timelapse_manager.db.models import Camera, Event, Project
from timelapse_manager.db.session import session_scope
from timelapse_manager.monitoring import NotificationDispatcher, get_active_alerts
from timelapse_manager.monitoring.channels import (
    NotificationChannel,
    NotificationMessage,
)
from timelapse_manager.monitoring.routing import evaluate_routing_rules


class _RecordingChannel(NotificationChannel):
    def __init__(self) -> None:
        self.received: list[NotificationMessage] = []

    @property
    def name(self) -> str:
        return "fake"

    async def send(self, message: NotificationMessage) -> None:
        self.received.append(message)


def _supervisor(factory: Any, tmp_path: Path) -> CaptureSupervisor:
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    settings = Settings(
        database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'test.db'}"),
        logging=LoggingSettings(level="WARNING", format="text"),
        paths=PathsSettings(
            data_dir=data_dir,
            frames_root=data_dir / "frames",
            token_file=data_dir / ".local-token",
        ),
        capture=CaptureSettings(autostart=False),
    )
    return CaptureSupervisor(settings, factory)


def _dispatcher(factory: Any, channel: _RecordingChannel) -> NotificationDispatcher:
    return NotificationDispatcher(
        session_factory=factory,
        channels=[channel],
        settings=MonitoringSettings(
            autostart=False,
            poll_interval_seconds=0.1,
            max_retries=1,
            retry_backoff_seconds=0.0,
            debounce_window_seconds=60.0,
            channel_send_timeout_seconds=30.0,
        ),
        routing_rules_fn=lambda: [
            {"event_types": ["all"], "min_level": "warning", "channels": ["fake"]}
        ],
    )


def _seed_camera(factory: Any, name: str = "Driveway") -> int:
    with session_scope(factory) as session:
        cam = Camera(name=name, address="10.0.16.71", protocol="vapix")
        session.add(cam)
        session.flush()
        session.add(
            Project(
                camera_id=cam.id,
                name=f"{name}-noon",
                lifecycle_state="active",
                operational_status="idle",
            )
        )
        return cam.id


def _event(
    factory: Any,
    *,
    scope: str,
    scope_id: int | None,
    reason: str,
    level: str = "warning",
    cleared: bool = False,
) -> int:
    with session_scope(factory) as session:
        row = Event(
            scope=scope,
            scope_id=scope_id,
            level=level,
            message=f"{scope} {reason}",
            timestamp=datetime.now(UTC).replace(tzinfo=None),
            event_metadata={"reason": reason},
            alert_cleared_at=(
                datetime.now(UTC).replace(tzinfo=None) if cleared else None
            ),
            alert_clear_reason="manual" if cleared else None,
        )
        session.add(row)
        session.flush()
        return row.id


def _row(factory: Any, event_id: int) -> tuple[Any, str | None]:
    with session_scope(factory) as session:
        row = session.get(Event, event_id)
        assert row is not None
        return row.alert_cleared_at, row.alert_clear_reason


def _system_reasons(factory: Any) -> list[str]:
    with session_scope(factory) as session:
        rows = session.query(Event).filter_by(scope="system").order_by(Event.id).all()
        return [str((row.event_metadata or {}).get("reason")) for row in rows]


class TestUpgradeSweep:
    async def test_project_scoped_offline_alerts_are_cleared_once(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        camera_id = _seed_camera(migrated_factory)
        old_a = _event(
            migrated_factory, scope="project", scope_id=1, reason="camera_offline"
        )
        old_b = _event(
            migrated_factory, scope="project", scope_id=2, reason="camera_offline"
        )
        already = _event(
            migrated_factory,
            scope="project",
            scope_id=3,
            reason="camera_offline",
            cleared=True,
        )
        low_disk = _event(
            migrated_factory, scope="project", scope_id=1, reason="low_disk"
        )
        camera_alert = _event(
            migrated_factory,
            scope="camera",
            scope_id=camera_id,
            reason="camera_offline",
        )

        supervisor = _supervisor(migrated_factory, tmp_path)
        await supervisor._ensure_camera_health_synced()
        await supervisor.stop()

        for event_id in (old_a, old_b):
            cleared_at, reason = _row(migrated_factory, event_id)
            assert cleared_at is not None
            assert reason == "auto"
        assert _row(migrated_factory, already)[1] == "manual"
        assert _row(migrated_factory, low_disk) == (None, None)
        assert _row(migrated_factory, camera_alert) == (None, None)
        assert supervisor._camera_health.is_offline(camera_id)
        assert _system_reasons(migrated_factory) == ["camera_offline_alerts_migrated"]
        with session_scope(migrated_factory) as session:
            info = session.query(Event).filter_by(scope="system").one()
            assert info.level == "info"
            assert (info.event_metadata or {})["cleared"] == 2

        # A second start (and the reconcile ticks of this one) do nothing more.
        again = _supervisor(migrated_factory, tmp_path)
        await again._ensure_camera_health_synced()
        await again._ensure_camera_health_synced()
        await again.stop()
        assert _system_reasons(migrated_factory) == ["camera_offline_alerts_migrated"]
        assert again._camera_health.is_offline(camera_id)

    async def test_nothing_to_sweep_writes_nothing(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        supervisor = _supervisor(migrated_factory, tmp_path)
        await supervisor._ensure_camera_health_synced()
        await supervisor.stop()
        assert supervisor._camera_health_synced
        assert _system_reasons(migrated_factory) == []

    async def test_a_seed_failure_is_retried_on_the_next_reconcile(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        from unittest.mock import patch

        camera_id = _seed_camera(migrated_factory)
        _event(
            migrated_factory,
            scope="camera",
            scope_id=camera_id,
            reason="camera_offline",
        )
        supervisor = _supervisor(migrated_factory, tmp_path)
        real = supervisor._sync_camera_health
        with patch.object(
            supervisor, "_sync_camera_health", side_effect=RuntimeError("locked")
        ):
            await supervisor._ensure_camera_health_synced()
        assert not supervisor._camera_health_synced
        with patch.object(supervisor, "_sync_camera_health", new=real):
            await supervisor._ensure_camera_health_synced()
        await supervisor.stop()
        assert supervisor._camera_health.is_offline(camera_id)


class TestCameraScopedAlertLifecycle:
    async def test_recovery_clears_only_that_cameras_alert_and_routes_alike(
        self, migrated_factory: Any, tmp_path: Path
    ) -> None:
        pano = _seed_camera(migrated_factory, "Driveway")
        door = _seed_camera(migrated_factory, "Porch")
        # A project-scoped alert whose scope_id happens to equal the camera id
        # must not be touched by the camera-scoped recovery.
        project_alert = _event(
            migrated_factory, scope="project", scope_id=pano, reason="low_disk"
        )
        supervisor = _supervisor(migrated_factory, tmp_path)
        for camera_id in (pano, door):
            supervisor._write_camera_health_event(
                HealthTransition(
                    kind="offline",
                    camera_id=camera_id,
                    consecutive_failures=3,
                    error="connect timeout",
                )
            )
        channel = _RecordingChannel()
        dispatcher = _dispatcher(migrated_factory, channel)
        await dispatcher.run_once()
        supervisor._write_camera_health_event(
            HealthTransition(kind="recovered", camera_id=pano)
        )
        await dispatcher.run_once()
        await supervisor.stop()

        # Both camera alerts were delivered by the scope-agnostic rule.
        delivered = [
            (m.scope, m.scope_id)
            for m in channel.received
            if (m.metadata or {}).get("reason") == "camera_offline"
        ]
        assert delivered == [("camera", pano), ("camera", door)]
        with session_scope(migrated_factory) as session:
            alerts, _ = get_active_alerts(session)
            active = sorted((a.scope, a.scope_id, a.reason) for a in alerts)
        assert active == [
            ("camera", door, "camera_offline"),
            ("project", pano, "low_disk"),
        ]
        assert _row(migrated_factory, project_alert) == (None, None)


class TestRoutingIgnoresScope:
    def test_a_rule_matches_on_type_and_level_only(self) -> None:
        rules = [
            {"event_types": ["all"], "min_level": "warning", "channels": ["email"]}
        ]
        # evaluate_routing_rules takes no scope at all: a camera-scoped warning
        # routes exactly as a project-scoped one did.
        assert evaluate_routing_rules("", "warning", rules) == {"email"}
        assert evaluate_routing_rules("", "info", rules) == set()
