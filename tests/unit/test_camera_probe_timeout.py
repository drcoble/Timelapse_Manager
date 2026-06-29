"""A slow camera probe must fail fast instead of hanging the request.

The settings page enumerates a camera's stream profiles, PTZ presets and event
topics inline while rendering. Against an unreachable camera each probe would
otherwise wait the full per-request network timeout, holding the request's
database connection for tens of seconds and starving concurrent requests. Each
probe is bounded by ``_CAMERA_PROBE_TIMEOUT_SECONDS``; these tests prove a probe
that overruns the bound resolves quickly to the degraded ``ok=False`` result and
still closes the adapter.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from timelapse_manager.web.routers import _shared


class _SlowAdapter:
    """An adapter whose every listing call sleeps far past the probe bound."""

    def __init__(self) -> None:
        self.closed = False

    async def list_event_topics(self) -> list[object]:
        await asyncio.sleep(10)
        return []

    async def list_stream_profiles(self) -> object:  # pragma: no cover - timed out
        await asyncio.sleep(10)
        raise AssertionError("should have timed out")

    async def list_ptz_presets(self) -> object:  # pragma: no cover - timed out
        await asyncio.sleep(10)
        raise AssertionError("should have timed out")

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def _patched_probe(monkeypatch: pytest.MonkeyPatch) -> _SlowAdapter:
    """Wire the probe helpers to a slow adapter with a tiny timeout bound."""
    adapter = _SlowAdapter()
    supervisor = SimpleNamespace(http_client=object(), ffmpeg_binary="ffmpeg")

    monkeypatch.setattr(_shared, "_CAMERA_PROBE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(
        _shared, "get_context", lambda: SimpleNamespace(capture_supervisor=supervisor)
    )
    monkeypatch.setattr(_shared, "resolve_default_credentials", lambda _db: None)
    # build_adapter is imported inside the helper from the cameras package.
    monkeypatch.setattr(
        "timelapse_manager.cameras.build_adapter",
        lambda *a, **k: adapter,
    )
    return adapter


def _camera() -> SimpleNamespace:
    # No address -> the SSRF resolve is skipped, isolating the probe-timeout path.
    return SimpleNamespace(address=None)


async def test_event_topics_probe_times_out_fast(_patched_probe: _SlowAdapter) -> None:
    result = await _shared._enumerate_event_topics(MagicMock(), _camera())
    assert result.ok is False
    assert result.events == []
    assert result.message == "unreachable"
    assert _patched_probe.closed is True


async def test_stream_profile_probe_times_out_fast(
    _patched_probe: _SlowAdapter,
) -> None:
    result = await _shared._enumerate_stream_profiles(MagicMock(), _camera())
    assert result.ok is False
    assert result.profiles == []
    assert _patched_probe.closed is True


async def test_ptz_preset_probe_times_out_fast(_patched_probe: _SlowAdapter) -> None:
    result = await _shared._enumerate_ptz_presets(MagicMock(), _camera())
    assert result.ok is False
    assert result.ptz_supported is False
    assert _patched_probe.closed is True
