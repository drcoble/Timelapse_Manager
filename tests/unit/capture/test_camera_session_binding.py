"""Narrowing the allowed subnets drops sessions holding the default (#223, #248).

A session loaded while a camera's hosts were inside
``ssrf.allowed_private_subnets`` keeps the global default login. It must stop
using it once the policy no longer covers those hosts, without the reconcile
fingerprint changing (so no reconcile pass drops every session).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from typing import Any

from timelapse_manager.capture.camera_config import CaptureCameraConfig
from timelapse_manager.capture.camera_sessions import CameraSessionRegistry

_DEFAULT = ("fleet", "fleet-pw")
_LAN = ["10.0.16.0/24"]


def _config(*, version: str = "v1", **overrides: Any) -> CaptureCameraConfig:
    values: dict[str, Any] = {
        "protocol": "vapix",
        "address": "10.0.16.5",
        "credentials": None,
        "credentials_inherit_default": True,
        "snapshot_uri": None,
        "stream_uri": None,
        "default_resolution": None,
        "default_credentials": _DEFAULT,
        "config_version": version,
    }
    values.update(overrides)
    return CaptureCameraConfig(**values)


async def _inline(fn: Callable[..., Any], *args: Any) -> Any:
    return fn(*args)


class _Policy:
    """The live subnet list, changed by the test like an administrator would."""

    def __init__(self, *subnets: str) -> None:
        self.subnets = list(subnets)

    def __call__(self) -> list[str]:
        return list(self.subnets)


class _Loader:
    """Binds like the real loader: the default only while the policy covers it."""

    def __init__(self, policy: _Policy, **overrides: Any) -> None:
        self._policy = policy
        self._overrides = overrides
        self.calls = 0

    def __call__(self, camera_id: int) -> CaptureCameraConfig:
        self.calls += 1
        address = ipaddress.ip_address(_config().address or "")
        covered = any(
            address in ipaddress.ip_network(subnet) for subnet in self._policy.subnets
        )
        values = {"default_credentials": _DEFAULT if covered else None}
        values.update(self._overrides)
        return _config(**values)


async def test_narrowing_drops_the_session_and_withholds_the_default() -> None:
    policy = _Policy(*_LAN)
    loader = _Loader(policy)
    registry = CameraSessionRegistry(loader, _inline, allowed_subnets=policy)

    first = await registry.get(7)
    assert first.credentials == _DEFAULT
    assert (await registry.get(7)) is first  # unchanged policy: a cache hit
    assert loader.calls == 1

    policy.subnets = ["10.200.0.0/16"]  # narrowed: the camera is no longer covered
    # The session is dropped and the reload (which binds anew) withholds it.
    second = await registry.get(7)
    assert second is not first
    assert second.credentials is None
    assert loader.calls == 2


async def test_an_unchanged_policy_never_reloads_however_often_it_is_used() -> None:
    policy = _Policy(*_LAN)
    loader = _Loader(policy)
    registry = CameraSessionRegistry(loader, _inline, allowed_subnets=policy)
    for _ in range(5):
        await registry.get(7)
        registry.observe({7: "v1"})  # a reconcile pass with the same fingerprint
    assert loader.calls == 1


async def test_a_session_with_its_own_login_ignores_the_policy() -> None:
    policy = _Policy(*_LAN)
    own = {"username": "own", "password": "x"}
    loader = _Loader(policy, credentials=own, credentials_inherit_default=False)
    registry = CameraSessionRegistry(loader, _inline, allowed_subnets=policy)
    await registry.get(7)
    policy.subnets = []
    await registry.get(7)
    assert loader.calls == 1


async def test_a_session_without_the_default_is_left_alone() -> None:
    policy = _Policy(*_LAN)
    loader = _Loader(
        policy, credentials_inherit_default=False, default_credentials=None
    )
    registry = CameraSessionRegistry(loader, _inline, allowed_subnets=policy)
    await registry.get(7)
    policy.subnets = []
    await registry.get(7)
    assert loader.calls == 1


async def test_a_withheld_session_is_stable_after_the_reload() -> None:
    """The reload does not repeat on every use once the default is withheld."""
    policy = _Policy(*_LAN)
    loader = _Loader(policy)
    registry = CameraSessionRegistry(loader, _inline, allowed_subnets=policy)
    await registry.get(7)
    policy.subnets = []
    for _ in range(4):
        assert (await registry.get(7)).credentials is None
    assert loader.calls == 2
