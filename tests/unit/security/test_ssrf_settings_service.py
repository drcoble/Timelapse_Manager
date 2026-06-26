"""Unit tests for the SSRF settings service: CIDR normalisation and the
config-baseline union. The web round-trip and live-apply behaviour are covered in
tests/web/test_ssrf_settings_web.py."""

from __future__ import annotations

from timelapse_manager.security.ssrf_settings_service import (
    effective_subnets,
    normalise_subnets,
)


class TestNormaliseSubnets:
    def test_bare_host_becomes_slash_32(self) -> None:
        normalised, invalid = normalise_subnets(["10.1.16.30"])
        assert normalised == ["10.1.16.30/32"]
        assert invalid == []

    def test_host_with_prefix_collapses_to_network(self) -> None:
        normalised, invalid = normalise_subnets(["10.1.16.30/24"])
        assert normalised == ["10.1.16.0/24"]
        assert invalid == []

    def test_blank_lines_ignored_and_whitespace_trimmed(self) -> None:
        normalised, invalid = normalise_subnets(["", "  ", "  10.1.16.0/24  "])
        assert normalised == ["10.1.16.0/24"]
        assert invalid == []

    def test_duplicates_collapse_preserving_first_order(self) -> None:
        normalised, _ = normalise_subnets(
            ["10.1.16.0/24", "192.168.5.0/24", "10.1.16.0/24"]
        )
        assert normalised == ["10.1.16.0/24", "192.168.5.0/24"]

    def test_ipv6_accepted(self) -> None:
        normalised, invalid = normalise_subnets(["fd00::/8"])
        assert normalised == ["fd00::/8"]
        assert invalid == []

    def test_unparsable_entries_collected_as_invalid(self) -> None:
        normalised, invalid = normalise_subnets(["10.1.16.0/24", "nope", "999.0.0.1"])
        assert normalised == ["10.1.16.0/24"]
        assert invalid == ["nope", "999.0.0.1"]


class TestEffectiveSubnets:
    def test_union_baseline_first_then_db(self) -> None:
        assert effective_subnets(["10.1.40.0/24"], ["10.1.16.0/24"]) == [
            "10.1.40.0/24",
            "10.1.16.0/24",
        ]

    def test_overlap_deduped_baseline_wins_position(self) -> None:
        assert effective_subnets(
            ["10.1.40.0/24"], ["10.1.40.0/24", "10.1.16.0/24"]
        ) == ["10.1.40.0/24", "10.1.16.0/24"]

    def test_empty_sources(self) -> None:
        assert effective_subnets([], []) == []
