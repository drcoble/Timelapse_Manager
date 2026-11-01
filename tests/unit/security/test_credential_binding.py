"""Credentials go only to the hosts they belong to (#67-S3, #69 CAM-S5, #193).

Pins the pure binding rules in :mod:`timelapse_manager.security.credential_binding`:
host extraction (one parser, the one ``httpx`` connects with), the rule that
the global default reaches a camera only when **every** host it can send
credentials to resolves wholly inside ``allowed_private_subnets``, and the edit
rule that stored credentials do not follow a camera to a new host.
"""

from __future__ import annotations

import socket
from collections.abc import Iterator
from types import SimpleNamespace

import pytest

from timelapse_manager.security import credential_binding as binding
from timelapse_manager.security import ssrf
from timelapse_manager.security.credential_binding import (
    bound_default_credentials,
    camera_credential_targets,
    introduces_new_host,
    may_substitute_default,
    split_userinfo,
    target_host,
)


@pytest.fixture(autouse=True)
def _fresh_resolution_cache() -> Iterator[None]:
    """Each test resolves for itself: the cache is shared process-wide (#248)."""
    ssrf.clear_resolution_cache()
    yield
    ssrf.clear_resolution_cache()


_LAN = ["10.0.16.0/24"]
_DEFAULT = ("fleet", "fleet-pw")


def _fake_dns(monkeypatch: pytest.MonkeyPatch, table: dict[str, list[str]]) -> None:
    """Resolve names from ``table``; anything else does not resolve."""

    def getaddrinfo(host: str, *_args: object, **_kwargs: object) -> list[tuple]:
        if host not in table:
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in table[host]
        ]

    ssrf.clear_resolution_cache()
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


class TestTargetHost:
    @pytest.mark.parametrize(
        ("value", "host"),
        [
            ("10.0.16.61", "10.0.16.61"),
            ("10.0.16.61:8080", "10.0.16.61"),
            ("https://10.0.16.61", "10.0.16.61"),
            ("https://Cam.LAN./snap.jpg", "cam.lan"),
            ("rtsp://10.0.16.61:554/stream", "10.0.16.61"),
            ("http://[fe80::1]:80/x", "fe80::1"),
            ("https://u:p@10.0.16.61/x", "10.0.16.61"),
        ],
    )
    def test_extracts_the_connect_host(self, value: str, host: str) -> None:
        assert target_host(value) == host

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "   ",
            "http://evil\\@10.0.16.61/",  # backslash: parsers disagree on the host
            "http://10.0.16.61 /x",
            "http://10.0.16.61/\r\nX: y",
            "http://",
        ],
    )
    def test_refuses_what_parsers_could_read_differently(
        self, value: str | None
    ) -> None:
        assert target_host(value) is None

    def test_userinfo_can_be_refused(self) -> None:
        assert target_host("http://u:p@10.0.16.61/", allow_userinfo=False) is None
        assert target_host("http://10.0.16.61/", allow_userinfo=False) == "10.0.16.61"


class TestSplitUserinfo:
    def test_splits_at_the_last_at_and_decodes(self) -> None:
        clean, login = split_userinfo("rtsp://us%40er:p@ss@10.0.16.5:554/x?y=1")
        assert clean == "rtsp://10.0.16.5:554/x?y=1"
        assert login == ("us@er", "p@ss")

    def test_username_only(self) -> None:
        assert split_userinfo("https://token@host/x") == (
            "https://host/x",
            ("token", ""),
        )

    def test_bare_address(self) -> None:
        assert split_userinfo("u:p@10.0.16.5") == ("10.0.16.5", ("u", "p"))

    @pytest.mark.parametrize(
        "value", ["10.0.16.5", "https://h/a@b", "http://h/x?e=a@b"]
    )
    def test_an_at_after_the_authority_is_not_userinfo(self, value: str) -> None:
        assert split_userinfo(value) == (value, None)


class TestMaySubstituteDefault:
    def test_literal_inside_the_subnet(self) -> None:
        assert may_substitute_default(["https://10.0.16.61"], _LAN)

    def test_literal_outside_the_subnet(self) -> None:
        assert not may_substitute_default(["203.0.113.5"], _LAN)
        assert not may_substitute_default(["10.0.17.5"], _LAN)

    def test_no_allowed_subnets_means_never(self) -> None:
        assert not may_substitute_default(["10.0.16.61"], [])
        assert not may_substitute_default(["10.0.16.61"], ["not-a-cidr"])

    def test_every_host_must_qualify(self) -> None:
        assert not may_substitute_default(["10.0.16.61", "http://203.0.113.5/s"], _LAN)
        assert may_substitute_default(["10.0.16.61", None, "http://10.0.16.9/s"], _LAN)

    def test_nothing_to_bind_is_a_refusal(self) -> None:
        assert not may_substitute_default([], _LAN)
        assert not may_substitute_default([None, ""], _LAN)

    def test_unparsable_value_fails_closed(self) -> None:
        assert not may_substitute_default(["http://evil\\@10.0.16.61/"], _LAN)

    def test_name_resolving_inside(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _fake_dns(monkeypatch, {"cam.lan": ["10.0.16.61"]})
        assert may_substitute_default(["https://cam.lan"], _LAN)

    def test_one_address_outside_is_enough_to_refuse(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_dns(monkeypatch, {"cam.lan": ["10.0.16.61", "203.0.113.5"]})
        assert not may_substitute_default(["cam.lan"], _LAN)

    def test_unresolvable_name_fails_closed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_dns(monkeypatch, {})
        assert not may_substitute_default(["cam.lan"], _LAN)

    def test_ipv4_mapped_ipv6_is_normalised(self) -> None:
        assert may_substitute_default(["[::ffff:10.0.16.61]"], _LAN)
        assert not may_substitute_default(["[::ffff:203.0.113.5]"], _LAN)


class TestBoundDefaultCredentials:
    def test_released_inside_the_subnet(self) -> None:
        assert bound_default_credentials(["10.0.16.61"], _DEFAULT, _LAN) == _DEFAULT

    def test_withheld_outside_the_subnet(self) -> None:
        assert bound_default_credentials(["203.0.113.5"], _DEFAULT, _LAN) is None

    def test_no_default_stays_none(self) -> None:
        assert bound_default_credentials(["10.0.16.61"], None, _LAN) is None

    def test_reads_the_running_applications_subnets(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(binding, "_configured_subnets", lambda: ["203.0.113.0/24"])
        assert bound_default_credentials(["203.0.113.5"], _DEFAULT) == _DEFAULT
        assert bound_default_credentials(["10.0.16.61"], _DEFAULT) is None


class TestCameraCredentialTargets:
    def test_address_and_both_uris(self) -> None:
        camera = SimpleNamespace(
            address="https://10.0.16.61",
            snapshot_uri="http://10.0.16.62/s.jpg",
            stream_uri=None,
        )
        assert camera_credential_targets(camera) == [
            "https://10.0.16.61",
            "http://10.0.16.62/s.jpg",
            None,
        ]

    def test_a_stored_uri_on_another_host_withholds_the_default(self) -> None:
        camera = SimpleNamespace(
            address="10.0.16.61",
            snapshot_uri="http://203.0.113.5/s.jpg",
            stream_uri=None,
        )
        assert (
            bound_default_credentials(camera_credential_targets(camera), _DEFAULT, _LAN)
            is None
        )

    def test_an_http_camera_is_bound_by_its_snapshot_uri_alone(self) -> None:
        camera = SimpleNamespace(
            address=None, snapshot_uri="http://203.0.113.5/s.jpg", stream_uri=None
        )
        assert (
            bound_default_credentials(camera_credential_targets(camera), _DEFAULT, _LAN)
            is None
        )


class TestIntroducesNewHost:
    def test_same_host_other_scheme_or_port_is_not_new(self) -> None:
        assert not introduces_new_host(["10.0.16.61"], ["https://10.0.16.61:8443"])

    def test_a_new_address_is_new(self) -> None:
        assert introduces_new_host(["10.0.16.61"], ["10.0.16.99"])

    def test_a_new_uri_host_is_new(self) -> None:
        assert introduces_new_host(
            ["10.0.16.61", None, None],
            ["10.0.16.61", "http://203.0.113.5/s.jpg", None],
        )

    def test_dropping_a_host_is_not_new(self) -> None:
        assert not introduces_new_host(
            ["10.0.16.61", "http://10.0.16.61/s"], ["10.0.16.61"]
        )

    def test_unparsable_after_value_counts_as_new(self) -> None:
        assert introduces_new_host(["10.0.16.61"], ["http://evil\\@10.0.16.61/"])

    def test_blank_values_are_ignored(self) -> None:
        assert not introduces_new_host(["10.0.16.61"], [None, "", "10.0.16.61"])


class TestBindingUsesTheSharedResolutionCache:
    """The address binding decides on is the address a connection dials (#248)."""

    def test_binding_and_the_dial_read_one_cached_answer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        table = {"cam.lan": ["10.0.16.61"]}
        _fake_dns(monkeypatch, table)
        assert may_substitute_default(["https://cam.lan"], _LAN)

        # The name now answers with a public address (a rebind).
        table["cam.lan"] = ["203.0.113.5"]
        dialled = asyncio.run(
            ssrf.pinned_addresses_async(
                "cam.lan", allow_private=True, allowed_private_subnets=_LAN
            )
        )
        # The connection dials what binding checked, not a second lookup ...
        assert dialled == ("10.0.16.61",)
        # ... and binding agrees with itself while that answer is cached.
        assert may_substitute_default(["https://cam.lan"], _LAN)

    def test_a_fresh_resolution_outside_the_subnets_is_not_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_dns(monkeypatch, {"cam.lan": ["203.0.113.5"]})
        assert not may_substitute_default(["https://cam.lan"], _LAN)

    def test_binding_populates_the_cache_the_dial_uses(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_dns(monkeypatch, {"cam.lan": ["10.0.16.61"]})
        assert may_substitute_default(["cam.lan"], _LAN)
        assert ssrf.resolved_ips_for(
            "https://cam.lan/", allow_private=True, allowed_private_subnets=_LAN
        ) == ("10.0.16.61",)

    def test_a_denied_address_is_never_bound(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _fake_dns(monkeypatch, {"cam.lan": ["127.0.0.1"]})
        assert not may_substitute_default(["cam.lan"], ["127.0.0.0/8"])


class TestUsesBoundDefault:
    @staticmethod
    def _config(**overrides: object) -> SimpleNamespace:
        values: dict[str, object] = {
            "credentials": None,
            "credentials_inherit_default": True,
            "default_credentials": _DEFAULT,
            "address": "10.0.16.61",
            "snapshot_uri": None,
            "stream_uri": None,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_an_inheriting_camera_holding_the_default(self) -> None:
        assert binding.uses_bound_default(self._config())

    def test_not_with_a_login_of_its_own(self) -> None:
        assert not binding.uses_bound_default(
            self._config(credentials={"username": "own", "password": "x"})
        )

    def test_not_when_the_default_was_withheld_or_not_inherited(self) -> None:
        assert not binding.uses_bound_default(self._config(default_credentials=None))
        assert not binding.uses_bound_default(
            self._config(credentials_inherit_default=False)
        )


class TestDefaultStillBound:
    async def test_inside_the_subnets_it_holds(self) -> None:
        config = TestUsesBoundDefault._config()
        assert await binding.default_still_bound(config, _LAN)

    async def test_narrowing_the_subnets_releases_it(self) -> None:
        config = TestUsesBoundDefault._config()
        assert not await binding.default_still_bound(config, ["10.200.0.0/16"])
        assert not await binding.default_still_bound(config, [])

    async def test_every_credential_target_must_stay_inside(self) -> None:
        config = TestUsesBoundDefault._config(snapshot_uri="http://203.0.113.5/s.jpg")
        assert not await binding.default_still_bound(config, _LAN)

    async def test_a_name_that_now_answers_outside_does_not_hold(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        table = {"cam.lan": ["10.0.16.61"]}
        _fake_dns(monkeypatch, table)
        config = TestUsesBoundDefault._config(address="https://cam.lan")
        assert await binding.default_still_bound(config, _LAN)
        ssrf.clear_resolution_cache()  # the cached answer ran out
        table["cam.lan"] = ["203.0.113.5"]
        assert not await binding.default_still_bound(config, _LAN)
