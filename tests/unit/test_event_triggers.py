"""Unit tests for the pure event-trigger parsing and matching module.

Covers:
- parse_triggers: validation, defaults, id generation, defensive canonicalisation
- serialize_trigger round-trip
- match_trigger: enabled/disabled, topic match (incl. prefix-shifted), rising
  edge (True), stateless (None) fire; falling edge (False) and unmatched/disabled
  do not.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from timelapse_manager.cameras.base import CameraEvent
from timelapse_manager.capture.event_triggers import (
    EventTrigger,
    match_trigger,
    parse_triggers,
    serialize_trigger,
)


def _event(topic_id: str, *, active: bool | None) -> CameraEvent:
    return CameraEvent(
        topic_id=topic_id,
        category="io",
        source={"port": "1"},
        data={"active": "1" if active else "0"},
        active=active,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw={},
    )


def _trigger(
    topic_id: str = "Device/IO/VirtualInput",
    *,
    enabled: bool = True,
    cooldown_seconds: int = 0,
    trigger_id: str = "t1",
) -> EventTrigger:
    return EventTrigger(
        id=trigger_id,
        topic_id=topic_id,
        label="Virtual Input",
        category="io",
        enabled=enabled,
        cooldown_seconds=cooldown_seconds,
    )


class TestParseTriggers:
    def test_none_and_empty_yield_no_triggers(self) -> None:
        assert parse_triggers(None) == []
        assert parse_triggers([]) == []

    def test_full_record_parsed(self) -> None:
        triggers = parse_triggers(
            [
                {
                    "id": "abc",
                    "topic_id": "Device/IO/VirtualInput",
                    "label": "Door",
                    "category": "io",
                    "enabled": True,
                    "cooldown_seconds": 30,
                }
            ]
        )
        assert triggers == [
            EventTrigger(
                id="abc",
                topic_id="Device/IO/VirtualInput",
                label="Door",
                category="io",
                enabled=True,
                cooldown_seconds=30,
            )
        ]

    def test_missing_id_generates_uuid_hex(self) -> None:
        (trigger,) = parse_triggers([{"topic_id": "Device/IO/VirtualInput"}])
        assert len(trigger.id) == 32
        assert int(trigger.id, 16)  # valid hex

    def test_blank_id_generates_uuid_hex(self) -> None:
        (trigger,) = parse_triggers(
            [{"id": "   ", "topic_id": "Device/IO/VirtualInput"}]
        )
        assert len(trigger.id) == 32

    def test_enabled_defaults_true_and_cooldown_defaults_zero(self) -> None:
        (trigger,) = parse_triggers([{"topic_id": "Device/IO/VirtualInput"}])
        assert trigger.enabled is True
        assert trigger.cooldown_seconds == 0

    def test_topic_is_canonicalised_defensively(self) -> None:
        (trigger,) = parse_triggers(
            [{"topic_id": "tns1:Device/tnsaxis:IO/VirtualInput"}]
        )
        assert trigger.topic_id == "Device/IO/VirtualInput"

    def test_not_a_list_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="must be a list"):
            parse_triggers({"topic_id": "x"})

    def test_non_mapping_record_raises_naming_index(self) -> None:
        with pytest.raises(ValueError, match=r"event_triggers\[0\] must be a mapping"):
            parse_triggers(["not-a-dict"])

    def test_missing_topic_raises_naming_field(self) -> None:
        with pytest.raises(ValueError, match=r"event_triggers\[0\].topic_id"):
            parse_triggers([{"label": "x"}])

    def test_blank_topic_raises(self) -> None:
        with pytest.raises(ValueError, match=r"topic_id"):
            parse_triggers([{"topic_id": "   "}])

    def test_negative_cooldown_raises_naming_field(self) -> None:
        with pytest.raises(ValueError, match=r"cooldown_seconds"):
            parse_triggers(
                [{"topic_id": "Device/IO/VirtualInput", "cooldown_seconds": -1}]
            )

    def test_non_int_cooldown_raises(self) -> None:
        with pytest.raises(ValueError, match=r"cooldown_seconds"):
            parse_triggers(
                [{"topic_id": "Device/IO/VirtualInput", "cooldown_seconds": "30"}]
            )

    def test_bool_cooldown_rejected(self) -> None:
        # bool is a subclass of int but is not a valid cooldown value.
        with pytest.raises(ValueError, match=r"cooldown_seconds"):
            parse_triggers(
                [{"topic_id": "Device/IO/VirtualInput", "cooldown_seconds": True}]
            )


class TestSerializeTrigger:
    def test_round_trip(self) -> None:
        trigger = _trigger(cooldown_seconds=15)
        (reparsed,) = parse_triggers([serialize_trigger(trigger)])
        assert reparsed == trigger


class TestMatchTrigger:
    def test_rising_edge_matches_enabled_trigger(self) -> None:
        triggers = [_trigger()]
        event = _event("Device/IO/VirtualInput", active=True)
        assert match_trigger(event, triggers) is triggers[0]

    def test_stateless_event_matches(self) -> None:
        triggers = [_trigger()]
        event = _event("Device/IO/VirtualInput", active=None)
        assert match_trigger(event, triggers) is triggers[0]

    def test_falling_edge_never_fires(self) -> None:
        triggers = [_trigger()]
        event = _event("Device/IO/VirtualInput", active=False)
        assert match_trigger(event, triggers) is None

    def test_disabled_trigger_does_not_match(self) -> None:
        triggers = [_trigger(enabled=False)]
        event = _event("Device/IO/VirtualInput", active=True)
        assert match_trigger(event, triggers) is None

    def test_unmatched_topic_returns_none(self) -> None:
        triggers = [_trigger(topic_id="Device/IO/VirtualInput")]
        event = _event("RuleEngine/MotionRegionDetector/Motion", active=True)
        assert match_trigger(event, triggers) is None

    def test_prefix_shifted_event_topic_matches(self) -> None:
        triggers = [_trigger(topic_id="Device/IO/VirtualInput")]
        event = _event("tns1:Device/tnsaxis:IO/VirtualInput", active=True)
        assert match_trigger(event, triggers) is triggers[0]

    def test_first_enabled_match_wins(self) -> None:
        first_disabled = _trigger(enabled=False, trigger_id="a")
        second_enabled = _trigger(enabled=True, trigger_id="b")
        event = _event("Device/IO/VirtualInput", active=True)
        assert match_trigger(event, [first_disabled, second_enabled]) is second_enabled

    def test_no_triggers_returns_none(self) -> None:
        event = _event("Device/IO/VirtualInput", active=True)
        assert match_trigger(event, []) is None
