"""A pinned client leaves the cache when no camera has its pin (#233).

A re-pin or an unpin used to leave the old certificate's client cached until
shutdown. The supervisor's reconcile pass now evicts it; an evicted client is
closed after a grace period so a request already using it finishes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from tests.unit.camera_pin_support import make_self_signed, pem_of
from timelapse_manager.cameras.clients import CameraHttpClients
from timelapse_manager.config.settings import CaptureSettings


def _pems(tmp_path: Path, count: int) -> list[str]:
    return [
        pem_of(make_self_signed(tmp_path / str(i), f"cam{i}")) for i in range(count)
    ]


async def test_a_replaced_pin_is_evicted_and_its_client_closed(tmp_path: Path) -> None:
    old, new = _pems(tmp_path, 2)
    clients = CameraHttpClients(CaptureSettings())
    clients.retire_grace_seconds = 0.0
    try:
        stale = clients.for_purpose("capture", old)
        clients.for_purpose("probe", old)
        kept = clients.for_purpose("capture", new)

        evicted = clients.retain_pinned([new])  # the camera was re-pinned to ``new``

        assert evicted == 2  # both purposes of the old pin
        assert clients.for_purpose("capture", new) is kept
        await asyncio.sleep(0.05)
        assert stale.is_closed
        assert not kept.is_closed
        # A camera pinned to the old certificate again gets a fresh client.
        assert clients.for_purpose("capture", old) is not stale
    finally:
        await clients.aclose()


async def test_an_unpinned_camera_leaves_nothing_cached(tmp_path: Path) -> None:
    (pem,) = _pems(tmp_path, 1)
    clients = CameraHttpClients(CaptureSettings())
    clients.retire_grace_seconds = 0.0
    try:
        client = clients.for_purpose("events", pem)
        assert clients.retain_pinned([]) == 1
        await asyncio.sleep(0.05)
        assert client.is_closed
        assert clients.retain_pinned([]) == 0  # idempotent
    finally:
        await clients.aclose()


async def test_a_pin_still_in_use_is_kept_and_junk_is_ignored(tmp_path: Path) -> None:
    (pem,) = _pems(tmp_path, 1)
    clients = CameraHttpClients(CaptureSettings())
    try:
        client = clients.for_purpose("capture", pem)
        assert clients.retain_pinned([pem, "not a certificate"]) == 0
        assert clients.for_purpose("capture", pem) is client
        assert not client.is_closed
    finally:
        await clients.aclose()


async def test_the_client_is_open_during_the_grace_period(tmp_path: Path) -> None:
    (pem,) = _pems(tmp_path, 1)
    clients = CameraHttpClients(CaptureSettings())
    clients.retire_grace_seconds = 30.0
    client = clients.for_purpose("capture", pem)
    clients.retain_pinned([])
    await asyncio.sleep(0.05)
    assert not client.is_closed  # an in-flight request would still complete
    await clients.aclose()  # shutdown closes it without waiting out the grace
    assert client.is_closed


async def test_the_supervisor_reconcile_evicts_after_a_repin(
    migrated_factory: Any, tmp_path: Path
) -> None:
    from tests.unit.test_campaign_bounds import (
        _make_settings,
        _permissive_disk_monitor,
    )
    from timelapse_manager.capture.supervisor import CaptureSupervisor
    from timelapse_manager.db.models import Camera
    from timelapse_manager.db.session import session_scope

    old, new = _pems(tmp_path, 2)
    with session_scope(migrated_factory) as session:
        camera = Camera(
            name="pinned",
            address="10.0.16.9",
            protocol="vapix",
            tls_pinned_cert_pem=old,
        )
        session.add(camera)
        session.flush()
        camera_id = camera.id
    supervisor = CaptureSupervisor(
        _make_settings(tmp_path),
        migrated_factory,
        disk_monitor=_permissive_disk_monitor(),
    )
    supervisor._clients.retire_grace_seconds = 0.0
    try:
        stale = supervisor.client_for("capture", old)
        await supervisor._evict_unused_pinned_clients()
        assert not stale.is_closed  # still pinned: kept

        with session_scope(migrated_factory) as session:
            session.get(Camera, camera_id).tls_pinned_cert_pem = new  # re-pin
        await supervisor._evict_unused_pinned_clients()
        await asyncio.sleep(0.05)
        assert stale.is_closed
    finally:
        await supervisor.stop()
