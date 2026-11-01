"""A pinned camera is captured, probed and listened to through its pinned client.

The pin (#69 CAM-S1) lives on the camera row. It must reach every place a
client is chosen: the capture config the camera session caches, the capture
and one-shot paths, and the event-listener factory. A pin change reloads the
camera's session, and a camera with no pin is exactly as before.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from sqlalchemy.orm import Session, sessionmaker

from tests.unit.camera_pin_support import make_self_signed, pem_of
from tests.unit.test_campaign_bounds import _make_settings, _permissive_disk_monitor
from timelapse_manager.cameras.tls import PinnedCameraClient
from timelapse_manager.capture.camera_config import config_version, load_capture_config
from timelapse_manager.capture.event_listener import EventListenerFactory
from timelapse_manager.capture.supervisor import CaptureSupervisor, CaptureTarget
from timelapse_manager.db.models import Camera
from timelapse_manager.db.session import session_scope

_BUILD = "timelapse_manager.capture.event_listener.build_adapter"


def _add_camera(factory: sessionmaker[Session], *, pem: str | None) -> int:
    with session_scope(factory) as session:
        camera = Camera(
            name="pinned" if pem else "plain",
            address="https://axis-b8a44f17384a",
            protocol="onvif",
            credentials_inherit_default=False,
            tls_pinned_cert_pem=pem,
        )
        session.add(camera)
        session.flush()
        return camera.id


class TestConfig:
    def test_the_loader_carries_the_pin(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        pem = pem_of(make_self_signed(tmp_path))
        pinned = load_capture_config(
            migrated_factory, _add_camera(migrated_factory, pem=pem)
        )
        plain = load_capture_config(
            migrated_factory, _add_camera(migrated_factory, pem=None)
        )
        assert pinned is not None and plain is not None
        assert pinned.tls_pinned_cert_pem == pem
        assert plain.tls_pinned_cert_pem is None

    def test_an_empty_stored_pin_reads_as_no_pin(
        self, migrated_factory: sessionmaker[Session]
    ) -> None:
        config = load_capture_config(
            migrated_factory, _add_camera(migrated_factory, pem="")
        )
        assert config is not None and config.tls_pinned_cert_pem is None

    def test_a_pin_change_changes_the_session_fingerprint(self, tmp_path: Path) -> None:
        first = pem_of(make_self_signed(tmp_path / "a", "a"))
        second = pem_of(make_self_signed(tmp_path / "b", "b"))
        base: dict[str, Any] = {
            "protocol": "vapix",
            "address": "https://cam",
            "credentials": None,
            "credentials_inherit_default": False,
            "snapshot_uri": None,
            "stream_uri": None,
            "default_resolution": None,
            "allow_basic_over_http": False,
        }
        unpinned = config_version(
            SimpleNamespace(**base, tls_pinned_cert_pem=None), None
        )
        one = config_version(SimpleNamespace(**base, tls_pinned_cert_pem=first), None)
        two = config_version(SimpleNamespace(**base, tls_pinned_cert_pem=second), None)
        assert len({unpinned, one, two}) == 3
        # Unpinned cameras keep their pre-pin fingerprint: no reload on upgrade.
        assert unpinned == config_version(SimpleNamespace(**base), None)


class TestSupervisorClient:
    async def test_pinned_camera_gets_the_pinned_capture_client(
        self, tmp_path: Path
    ) -> None:
        pem = pem_of(make_self_signed(tmp_path))
        supervisor = CaptureSupervisor(
            _make_settings(tmp_path),
            MagicMock(),
            disk_monitor=_permissive_disk_monitor(),
        )
        try:
            pinned = supervisor.capture_client_for(
                SimpleNamespace(tls_pinned_cert_pem=pem)
            )
            assert isinstance(pinned, PinnedCameraClient)
            assert pinned is supervisor.client_for("capture", pem)
            for config in (
                SimpleNamespace(tls_pinned_cert_pem=None),
                SimpleNamespace(tls_pinned_cert_pem=""),
                SimpleNamespace(),
            ):
                assert supervisor.capture_client_for(config) is supervisor.http_client
        finally:
            await supervisor.stop()

    async def test_capture_once_builds_its_adapter_on_the_pinned_client(
        self, migrated_factory: sessionmaker[Session], tmp_path: Path
    ) -> None:
        pem = pem_of(make_self_signed(tmp_path))
        camera_id = _add_camera(migrated_factory, pem=pem)
        supervisor = CaptureSupervisor(
            _make_settings(tmp_path),
            migrated_factory,
            disk_monitor=_permissive_disk_monitor(),
        )
        used: list[httpx.AsyncClient] = []

        class _Stop(Exception):
            pass

        def fake_build(config: Any, client: httpx.AsyncClient, **kwargs: Any) -> Any:
            used.append(client)
            raise _Stop

        target = CaptureTarget(
            project_id=1, project_name="p", camera_id=camera_id, interval_seconds=None
        )
        try:
            with (
                patch("timelapse_manager.capture.supervisor.build_adapter", fake_build),
                contextlib.suppress(_Stop),
            ):
                await supervisor._capture_once(target, MagicMock())
            assert len(used) == 1
            assert used[0] is supervisor.client_for("capture", pem)
            assert used[0] is not supervisor.http_client
        finally:
            await supervisor.stop()


class TestEventListener:
    async def _open(
        self, factory: EventListenerFactory, config: Any
    ) -> httpx.AsyncClient:
        seen: list[httpx.AsyncClient] = []

        async def _parked() -> AsyncIterator[Any]:
            await asyncio.Event().wait()
            yield  # pragma: no cover

        def fake_build(cfg: Any, client: httpx.AsyncClient, **kwargs: Any) -> Any:
            seen.append(client)
            adapter = MagicMock()
            adapter.open_event_source = MagicMock(side_effect=_parked)
            adapter.close = AsyncMock()
            return adapter

        with patch(_BUILD, fake_build):
            source = factory._onvif_pullpoint_events(config, None)
            task = asyncio.create_task(anext(source))
            for _ in range(200):
                if seen:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await source.aclose()
        return seen[0]

    async def test_events_use_the_pinned_client_only_for_a_pinned_camera(
        self, tmp_path: Path
    ) -> None:
        pem = pem_of(make_self_signed(tmp_path))
        supervisor = CaptureSupervisor(
            _make_settings(tmp_path),
            MagicMock(),
            disk_monitor=_permissive_disk_monitor(),
        )
        shared = supervisor.client_for("events")
        factory = EventListenerFactory(
            shared,
            MagicMock(),
            pinned_client_for=lambda p: supervisor.client_for("events", p),
        )
        try:
            pinned = await self._open(
                factory, SimpleNamespace(protocol="onvif", tls_pinned_cert_pem=pem)
            )
            plain = await self._open(
                factory, SimpleNamespace(protocol="onvif", tls_pinned_cert_pem=None)
            )
            assert isinstance(pinned, PinnedCameraClient)
            assert pinned is supervisor.client_for("events", pem)
            assert plain is shared
        finally:
            await supervisor.stop()

    async def test_without_a_resolver_a_factory_keeps_its_one_client(self) -> None:
        shared = MagicMock()
        factory = EventListenerFactory(shared, MagicMock())
        config = SimpleNamespace(tls_pinned_cert_pem="whatever")
        assert factory._client_for(config) is shared
