"""One trusted-proxy parser for the web layer and the token peer policy (#174)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest
from starlette.requests import Request

from timelapse_manager.security import proxy_networks, token
from timelapse_manager.web import middleware


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    proxy_networks.parse_networks_cached.cache_clear()
    yield
    proxy_networks.parse_networks_cached.cache_clear()


def _request(peer: str, headers: dict[str, str]) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/projects",
            "scheme": "https",
            "query_string": b"",
            "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()],
            "client": (peer, 5000),
        }
    )


class TestSharedImplementation:
    def test_web_layer_and_token_policy_use_the_same_functions(self) -> None:
        assert middleware._parse_networks_cached is proxy_networks.parse_networks_cached
        assert middleware._parse_address is proxy_networks.parse_address
        assert middleware._in_networks is proxy_networks.in_networks
        assert token.parse_address is proxy_networks.parse_address
        assert token.in_networks is proxy_networks.in_networks

    def test_parse_is_cached_per_entry_list(self) -> None:
        first = proxy_networks.parse_networks(["192.0.2.10"])
        second = proxy_networks.parse_networks(["192.0.2.10"])
        assert first == second
        assert proxy_networks.parse_networks_cached.cache_info().hits == 1

    def test_ipv4_mapped_peer_folds_to_ipv4(self) -> None:
        mapped = proxy_networks.parse_address("::ffff:192.0.2.10")
        assert str(mapped) == "192.0.2.10"
        assert proxy_networks.parse_address(None) is None
        assert proxy_networks.parse_address("edgeproxy.example.com") is None


class TestMalformedEntryWarnsOnce:
    def test_token_peer_policy_logs_a_bad_trusted_proxy_entry_once(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        request = _request(
            "192.0.2.10",
            {"x-forwarded-for": "10.0.90.5", "x-forwarded-proto": "https"},
        )
        with caplog.at_level(logging.WARNING, logger=proxy_networks.logger.name):
            for _ in range(3):
                assert token.peer_allowed(
                    request,
                    ["10.0.90.0/24"],
                    trusted_proxies=["192.0.2.10", "edgeproxy.example.com"],
                    require_https=True,
                )
        warnings = [
            r.getMessage() for r in caplog.records if "edgeproxy" in r.getMessage()
        ]
        assert len(warnings) == 1
        assert "server.trusted_proxies" in warnings[0]


class TestProxyTrustIsUnchanged:
    """``X-Forwarded-*`` counts only from a configured trusted proxy."""

    def test_forwarded_client_from_the_trusted_proxy_is_honoured(self) -> None:
        request = _request(
            "192.0.2.10",
            {"x-forwarded-for": "10.0.90.5", "x-forwarded-proto": "https"},
        )
        assert token.peer_allowed(
            request,
            ["10.0.90.0/24"],
            trusted_proxies=["192.0.2.10"],
            require_https=True,
        )

    def test_forwarded_headers_from_any_other_peer_are_ignored(self) -> None:
        request = _request(
            "10.9.9.9",
            {"x-forwarded-for": "10.0.90.5", "x-forwarded-proto": "https"},
        )
        assert not token.peer_allowed(
            request,
            ["10.0.90.0/24"],
            trusted_proxies=["192.0.2.10"],
            require_https=True,
        )

    def test_a_malformed_entry_does_not_widen_trust(self) -> None:
        request = _request(
            "10.9.9.9", {"x-forwarded-for": "10.0.90.5", "x-forwarded-proto": "https"}
        )
        assert not token.peer_allowed(
            request,
            ["10.0.90.0/24"],
            trusted_proxies=["not-an-ip", "192.0.2.10"],
            require_https=True,
        )
