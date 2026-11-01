"""The async outbound-URL guard and its resolution cache (#67 P6).

``assert_allowed_url_async`` must give the same verdicts as the synchronous
guard (private, loopback and metadata targets are denied; an unresolvable host
is a denial), resolve off the event loop, and cache only allowed verdicts, for
60 s, keyed on the policy flags as well as the host.
"""

from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Iterator

import pytest

from timelapse_manager.render.post_actions import validate_outbound_url_async
from timelapse_manager.security import ssrf
from timelapse_manager.security.ssrf import (
    SsrfError,
    assert_allowed_url_async,
    clear_resolution_cache,
    resolved_ips_for,
)

pytestmark = pytest.mark.abuse

_RESOLVER = "timelapse_manager.security.ssrf.socket.getaddrinfo"


def _answer(*addresses: str) -> list[tuple[object, ...]]:
    return [(None, None, None, "", (address, 0)) for address in addresses]


class _Resolver:
    """A fake ``getaddrinfo`` that counts calls and answers from a table."""

    def __init__(self, answers: dict[str, list[str]]) -> None:
        self.answers = answers
        self.calls: list[str] = []

    def __call__(self, host: str, *args: object, **kwargs: object) -> list:
        self.calls.append(host)
        if host not in self.answers:
            raise socket.gaierror(socket.EAI_NONAME, "not found")
        return _answer(*self.answers[host])


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    clear_resolution_cache()
    yield
    clear_resolution_cache()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(ssrf._resolution_cache, "clock", fake)  # noqa: SLF001
    return fake


class TestVerdicts:
    @pytest.mark.parametrize(
        "address",
        ["10.0.12.5", "192.168.1.1", "127.0.0.1", "169.254.169.254", "::1"],
    )
    async def test_private_loopback_and_metadata_hosts_are_denied(
        self, monkeypatch: pytest.MonkeyPatch, address: str
    ) -> None:
        monkeypatch.setattr(_RESOLVER, _Resolver({"hook.example.test": [address]}))
        with pytest.raises(SsrfError):
            await assert_allowed_url_async("https://hook.example.test/x")

    @pytest.mark.parametrize(
        "url",
        [
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8080/api",
            "http://[::ffff:127.0.0.1]/",
            "http://10.0.0.1/",
        ],
    )
    async def test_denied_literal_ips_never_resolve(
        self, monkeypatch: pytest.MonkeyPatch, url: str
    ) -> None:
        resolver = _Resolver({})
        monkeypatch.setattr(_RESOLVER, resolver)
        with pytest.raises(SsrfError):
            await assert_allowed_url_async(url)
        assert resolver.calls == []

    async def test_one_private_address_among_public_ones_denies(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            _RESOLVER, _Resolver({"mixed.example.test": ["8.8.8.8", "10.0.0.7"]})
        )
        with pytest.raises(SsrfError):
            await assert_allowed_url_async("https://mixed.example.test/")

    async def test_unresolvable_host_is_a_denial(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_RESOLVER, _Resolver({}))
        with pytest.raises(SsrfError, match="did not resolve"):
            await assert_allowed_url_async("https://nowhere.example.test/")

    @pytest.mark.parametrize("url", ["https:///path", "https://host:99999/", "x"])
    async def test_malformed_url_raises_ssrf_error_not_value_error(
        self, url: str
    ) -> None:
        with pytest.raises(SsrfError):
            await assert_allowed_url_async(url)

    async def test_webhook_seam_never_opts_into_private_space(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_RESOLVER, _Resolver({"lan.example.test": ["10.0.16.9"]}))
        with pytest.raises(SsrfError):
            await validate_outbound_url_async("https://lan.example.test/")

    async def test_allowed_url_is_returned_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_RESOLVER, _Resolver({"hook.example.test": ["8.8.8.8"]}))
        url = "https://hook.example.test/notify?x=1"
        assert await validate_outbound_url_async(url) == url


class TestCache:
    async def test_allowed_verdict_is_reused_within_the_ttl(
        self, monkeypatch: pytest.MonkeyPatch, clock: _Clock
    ) -> None:
        resolver = _Resolver({"hook.example.test": ["8.8.8.8"]})
        monkeypatch.setattr(_RESOLVER, resolver)
        await assert_allowed_url_async("https://hook.example.test/a")
        clock.now += 59.0
        await assert_allowed_url_async("https://HOOK.example.test/b")
        assert resolver.calls == ["hook.example.test"]
        assert resolved_ips_for("https://hook.example.test/") == ("8.8.8.8",)

    async def test_cache_expires_after_the_ttl(
        self, monkeypatch: pytest.MonkeyPatch, clock: _Clock
    ) -> None:
        resolver = _Resolver({"hook.example.test": ["8.8.8.8"]})
        monkeypatch.setattr(_RESOLVER, resolver)
        await assert_allowed_url_async("https://hook.example.test/")
        clock.now += 61.0
        assert resolved_ips_for("https://hook.example.test/") is None
        # The name now points somewhere private: the fresh check denies it.
        resolver.answers["hook.example.test"] = ["10.0.0.5"]
        with pytest.raises(SsrfError):
            await assert_allowed_url_async("https://hook.example.test/")
        assert len(resolver.calls) == 2

    async def test_denials_are_not_cached(
        self, monkeypatch: pytest.MonkeyPatch, clock: _Clock
    ) -> None:
        resolver = _Resolver({"hook.example.test": ["10.0.0.5"]})
        monkeypatch.setattr(_RESOLVER, resolver)
        with pytest.raises(SsrfError):
            await assert_allowed_url_async("https://hook.example.test/")
        assert resolved_ips_for("https://hook.example.test/") is None
        resolver.answers["hook.example.test"] = ["8.8.8.8"]
        await assert_allowed_url_async("https://hook.example.test/")
        assert len(resolver.calls) == 2

    async def test_verdict_is_keyed_on_the_policy_flags(
        self, monkeypatch: pytest.MonkeyPatch, clock: _Clock
    ) -> None:
        """An allow under an admin subnet must not leak to a stricter caller."""
        resolver = _Resolver({"cam.example.test": ["10.0.16.9"]})
        monkeypatch.setattr(_RESOLVER, resolver)
        await assert_allowed_url_async(
            "https://cam.example.test/",
            allow_private=True,
            allowed_private_subnets=["10.0.16.0/24"],
        )
        with pytest.raises(SsrfError):
            await assert_allowed_url_async("https://cam.example.test/")
        with pytest.raises(SsrfError):
            await assert_allowed_url_async(
                "https://cam.example.test/",
                allow_private=True,
                allowed_private_subnets=["10.0.17.0/24"],
            )
        assert resolved_ips_for(
            "https://cam.example.test/",
            allow_private=True,
            allowed_private_subnets=[" 10.0.16.0/24"],
        ) == ("10.0.16.9",)
        assert resolved_ips_for("https://cam.example.test/") is None

    async def test_cache_is_bounded(
        self, monkeypatch: pytest.MonkeyPatch, clock: _Clock
    ) -> None:
        hosts = {f"h{i}.example.test": ["8.8.8.8"] for i in range(600)}
        monkeypatch.setattr(_RESOLVER, _Resolver(hosts))
        for host in hosts:
            await assert_allowed_url_async(f"https://{host}/")
        assert len(ssrf._resolution_cache) == 512  # noqa: SLF001
        assert resolved_ips_for("https://h0.example.test/") is None
        assert resolved_ips_for("https://h599.example.test/") == ("8.8.8.8",)

    def test_literal_ip_reports_itself(self) -> None:
        assert resolved_ips_for("https://8.8.4.4:8443/") == ("8.8.4.4",)
        assert resolved_ips_for("not a url") is None


class TestSlowResolverDoesNotStallTheLoop:
    async def test_two_second_getaddrinfo_causes_no_loop_lag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def slow_getaddrinfo(host: str, *args: object, **kwargs: object) -> list:
            time.sleep(2.0)
            return _answer("8.8.8.8")

        monkeypatch.setattr(_RESOLVER, slow_getaddrinfo)
        loop = asyncio.get_running_loop()
        worst_lag = 0.0
        done = asyncio.Event()

        async def ticker() -> None:
            nonlocal worst_lag
            while not done.is_set():
                before = loop.time()
                await asyncio.sleep(0.05)
                worst_lag = max(worst_lag, loop.time() - before - 0.05)

        ticking = asyncio.create_task(ticker())
        try:
            await assert_allowed_url_async("https://slow.example.test/")
        finally:
            done.set()
            await ticking
        assert worst_lag < 0.5, f"the event loop stalled for {worst_lag:.2f}s"
