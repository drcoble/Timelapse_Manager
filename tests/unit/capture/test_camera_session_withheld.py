"""A withheld default is re-decided on use, not latched (#269).

A hostname camera that inherits the global default can have it withheld because
its name did not resolve at that moment (or because the subnets were narrower).
The session built without the default is cached while the camera's rows are
unchanged, so before this fix the verdict stood until an edit or a restart, and
widening the subnets or DNS recovering restored nothing.
"""

from __future__ import annotations

import socket
from collections.abc import Callable, Iterator
from dataclasses import replace
from typing import Any

import pytest

from timelapse_manager.capture.camera_config import CaptureCameraConfig
from timelapse_manager.capture.camera_sessions import (
    WITHHELD_RECHECK_SECONDS,
    CameraSessionRegistry,
)
from timelapse_manager.security import credential_binding, ssrf
from timelapse_manager.security.credential_binding import (
    bound_default_credentials,
    camera_credential_targets,
)

_DEFAULT = ("fleet", "fleet-pw")
_NAME = "cam.lan"


class _Dns:
    """A resolver that can go down and come back, like a flapping DNS server."""

    def __init__(self) -> None:
        self.up = True
        self.answer = "10.0.16.5"

    def __call__(self, host: str, *_a: object, **_k: object) -> list[Any]:
        if not self.up:
            raise socket.gaierror(host)
        return [(2, 1, 6, "", (self.answer, 0))]


class _Clock:
    now = 100.0

    def __call__(self) -> float:
        return self.now

    def pass_recheck_interval(self) -> None:
        self.now += WITHHELD_RECHECK_SECONDS + 1


class _Policy:
    def __init__(self, *subnets: str) -> None:
        self.subnets = list(subnets)

    def __call__(self) -> list[str]:
        return list(self.subnets)


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Dns]:
    resolver = _Dns()
    monkeypatch.setattr(ssrf.socket, "getaddrinfo", resolver)
    ssrf.clear_resolution_cache()
    yield resolver
    ssrf.clear_resolution_cache()


class _Loader:
    """Binds exactly like :func:`load_capture_config` does after the DB read."""

    def __init__(self, policy: _Policy, **overrides: Any) -> None:
        self.policy = policy
        self.calls = 0
        self.overrides = overrides

    def __call__(self, camera_id: int) -> CaptureCameraConfig:
        self.calls += 1
        values: dict[str, Any] = {
            "protocol": "vapix",
            "address": _NAME,
            "credentials": None,
            "credentials_inherit_default": True,
            "snapshot_uri": None,
            "stream_uri": None,
            "default_resolution": None,
            "default_credentials": _DEFAULT,
            "config_version": "v1",
        }
        values.update(self.overrides)
        config = CaptureCameraConfig(**values)
        bound = bound_default_credentials(
            camera_credential_targets(config), _DEFAULT, self.policy()
        )
        return replace(
            config, default_credentials=bound, default_withheld=bound is None
        )


async def _inline(fn: Callable[..., Any], *args: Any) -> Any:
    return fn(*args)


async def test_a_dns_miss_then_success_restores_the_default(dns: _Dns) -> None:
    clock = _Clock()
    policy = _Policy("10.0.16.0/24")
    loader = _Loader(policy)
    registry = CameraSessionRegistry(
        loader, _inline, allowed_subnets=policy, clock=clock
    )

    assert (await registry.get(7)).credentials == _DEFAULT  # bound at first load

    dns.up = False  # the transient miss: the use-time check fails
    ssrf.clear_resolution_cache()
    withheld = await registry.get(7)
    assert withheld.credentials is None
    assert withheld.config.default_withheld is True

    # Still down: the verdict is re-asked each use but nothing is reloaded.
    loads = loader.calls
    assert (await registry.get(7)).credentials is None
    assert loader.calls == loads

    dns.up = True  # DNS recovers: no edit, no restart
    clock.pass_recheck_interval()
    restored = await registry.get(7)
    assert restored.credentials == _DEFAULT
    assert restored.config.default_withheld is False
    assert (await registry.get(7)) is restored  # and it is cached again


async def test_widening_the_subnets_restores_a_withheld_default(dns: _Dns) -> None:
    clock = _Clock()
    policy = _Policy("10.200.0.0/16")  # does not cover the camera
    loader = _Loader(policy)
    registry = CameraSessionRegistry(
        loader, _inline, allowed_subnets=policy, clock=clock
    )
    assert (await registry.get(7)).credentials is None

    policy.subnets = ["10.0.16.0/24"]
    ssrf.clear_resolution_cache()  # the cache is keyed by policy; be explicit
    clock.pass_recheck_interval()
    assert (await registry.get(7)).credentials == _DEFAULT


async def test_a_host_that_stays_outside_never_gets_the_default(dns: _Dns) -> None:
    clock = _Clock()
    dns.answer = "93.184.216.34"
    policy = _Policy("10.0.16.0/24")
    loader = _Loader(policy)
    registry = CameraSessionRegistry(
        loader, _inline, allowed_subnets=policy, clock=clock
    )
    for _ in range(5):
        assert (await registry.get(7)).credentials is None
    assert loader.calls == 1


async def test_own_credentials_are_not_a_withheld_session(dns: _Dns) -> None:
    clock = _Clock()
    policy = _Policy("10.0.16.0/24")
    loader = _Loader(
        policy,
        credentials={"username": "own", "password": "x"},
        credentials_inherit_default=False,
        default_credentials=None,
    )
    registry = CameraSessionRegistry(
        loader, _inline, allowed_subnets=policy, clock=clock
    )
    dns.up = False
    for _ in range(3):
        await registry.get(7)
    assert loader.calls == 1


async def test_a_withheld_camera_with_dead_dns_costs_one_lookup_per_interval(
    dns: _Dns, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failed lookups are not cached, so the re-check is rate-limited (#284)."""
    clock = _Clock()
    policy = _Policy("10.0.16.0/24")
    loader = _Loader(policy)
    registry = CameraSessionRegistry(
        loader, _inline, allowed_subnets=policy, clock=clock
    )
    dns.up = False
    lookups: list[str] = []
    real = dns.__call__

    def counting(host: str, *a: object, **k: object) -> list[Any]:
        lookups.append(host)
        return real(host, *a, **k)

    monkeypatch.setattr(ssrf.socket, "getaddrinfo", counting)
    await registry.get(7)  # loads withheld: the loader's own lookup(s)
    after_load = len(lookups)
    for _ in range(50):  # many uses inside one interval
        assert (await registry.get(7)).credentials is None
    assert len(lookups) == after_load  # no lookup at all on those uses

    clock.pass_recheck_interval()
    for _ in range(50):
        await registry.get(7)
    assert len(lookups) == after_load + 1  # exactly one re-check
    assert loader.calls == 1


def test_the_helper_ignores_a_config_that_was_not_withheld() -> None:
    plain = CaptureCameraConfig(
        protocol="vapix",
        address=_NAME,
        credentials=None,
        credentials_inherit_default=True,
        snapshot_uri=None,
        stream_uri=None,
        default_resolution=None,
    )
    assert not credential_binding.default_withheld(plain)
    assert credential_binding.default_withheld(replace(plain, default_withheld=True))
