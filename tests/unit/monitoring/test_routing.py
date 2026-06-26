"""Unit tests for the pure routing function evaluate_routing_rules.

Tests the full routing matrix: type matching, wildcard, min_level floor,
no-match empty result, and loop-prevention for notify.delivery_failed.
"""

from __future__ import annotations

from timelapse_manager.monitoring import EventType, evaluate_routing_rules


def _rule(
    *,
    event_types: list[str],
    min_level: str | None = None,
    channels: list[str],
) -> dict:
    rule: dict = {"event_types": event_types, "channels": channels}
    if min_level is not None:
        rule["min_level"] = min_level
    return rule


class TestTypeMatcher:
    def test_exact_type_match_routes_to_channel(self) -> None:
        rules = [_rule(event_types=["camera.offline_threshold"], channels=["email"])]
        result = evaluate_routing_rules("camera.offline_threshold", "warning", rules)
        assert result == {"email"}

    def test_wrong_type_does_not_match(self) -> None:
        rules = [_rule(event_types=["render.failed"], channels=["email"])]
        result = evaluate_routing_rules("capture.gap", "warning", rules)
        assert result == set()

    def test_all_wildcard_matches_any_type(self) -> None:
        rules = [_rule(event_types=["all"], channels=["webhook"])]
        result = evaluate_routing_rules("storage.disk_low", "info", rules)
        assert result == {"webhook"}

    def test_all_wildcard_matches_an_unknown_event_type(self) -> None:
        rules = [_rule(event_types=["all"], channels=["email"])]
        result = evaluate_routing_rules("future.event_type", "info", rules)
        assert result == {"email"}

    def test_non_list_event_types_is_ignored(self) -> None:
        rules = [{"event_types": "camera.reconnect", "channels": ["email"]}]
        result = evaluate_routing_rules("camera.reconnect", "info", rules)
        assert result == set()


class TestMinLevelFloor:
    def test_event_at_exact_min_level_matches(self) -> None:
        rules = [_rule(event_types=["all"], min_level="warning", channels=["email"])]
        result = evaluate_routing_rules("capture.gap", "warning", rules)
        assert result == {"email"}

    def test_event_above_min_level_matches(self) -> None:
        rules = [_rule(event_types=["all"], min_level="warning", channels=["email"])]
        result = evaluate_routing_rules("capture.gap", "critical", rules)
        assert result == {"email"}

    def test_event_below_min_level_does_not_match(self) -> None:
        rules = [_rule(event_types=["all"], min_level="error", channels=["email"])]
        result = evaluate_routing_rules("capture.gap", "info", rules)
        assert result == set()

    def test_rule_without_min_level_matches_any_level(self) -> None:
        rules = [_rule(event_types=["all"], channels=["webhook"])]
        result = evaluate_routing_rules("capture.gap", "info", rules)
        assert result == {"webhook"}

    def test_unknown_min_level_in_rule_treats_as_no_floor(self) -> None:
        """A malformed/unknown min_level imposes no floor — rule always matches."""
        rules = [_rule(event_types=["all"], min_level="bogus", channels=["webhook"])]
        result = evaluate_routing_rules("capture.gap", "info", rules)
        assert result == {"webhook"}


class TestChannelUnion:
    def test_multiple_matching_rules_union_their_channels(self) -> None:
        rules = [
            _rule(event_types=["all"], min_level="info", channels=["email"]),
            _rule(event_types=["capture.gap"], min_level="info", channels=["webhook"]),
        ]
        result = evaluate_routing_rules("capture.gap", "info", rules)
        assert result == {"email", "webhook"}

    def test_channels_deduplicated_across_rules(self) -> None:
        rules = [
            _rule(event_types=["all"], channels=["email"]),
            _rule(event_types=["capture.gap"], channels=["email"]),
        ]
        result = evaluate_routing_rules("capture.gap", "info", rules)
        assert result == {"email"}

    def test_non_matching_rule_channel_not_included(self) -> None:
        rules = [
            _rule(event_types=["render.complete"], channels=["email"]),
            _rule(event_types=["capture.gap"], channels=["webhook"]),
        ]
        result = evaluate_routing_rules("capture.gap", "info", rules)
        assert result == {"webhook"}


class TestNoMatchConditions:
    def test_empty_rule_list_returns_empty_set(self) -> None:
        result = evaluate_routing_rules("capture.gap", "info", [])
        assert result == set()

    def test_non_list_routing_rules_returns_empty_set(self) -> None:
        result = evaluate_routing_rules("capture.gap", "info", "not-a-list")  # type: ignore[arg-type]
        assert result == set()

    def test_non_dict_rule_entries_are_skipped(self) -> None:
        rules = ["bad-entry", _rule(event_types=["capture.gap"], channels=["email"])]
        result = evaluate_routing_rules("capture.gap", "info", rules)
        assert result == {"email"}

    def test_missing_channels_key_contributes_nothing(self) -> None:
        rules = [{"event_types": ["all"]}]
        result = evaluate_routing_rules("capture.gap", "info", rules)
        assert result == set()


class TestLoopPrevention:
    def test_notify_delivery_failed_not_routed_regardless_of_all_rule(self) -> None:
        """notify.delivery_failed must NEVER be routed to any channel.

        A matching rule with wildcard type and low min_level would normally
        capture every event. Routing hard-excludes the delivery-failure type
        so a channel failure can never trigger another notification attempt.
        """
        rules = [_rule(event_types=["all"], min_level="info", channels=["email"])]
        result = evaluate_routing_rules(
            EventType.NOTIFY_DELIVERY_FAILED.value, "error", rules
        )
        assert result == set()

    def test_notify_delivery_failed_not_routed_with_explicit_type_rule(self) -> None:
        """Even an explicitly-named rule cannot route the delivery-failure event."""
        rules = [
            _rule(
                event_types=[EventType.NOTIFY_DELIVERY_FAILED.value],
                channels=["webhook"],
            )
        ]
        result = evaluate_routing_rules(
            EventType.NOTIFY_DELIVERY_FAILED.value, "error", rules
        )
        assert result == set()
