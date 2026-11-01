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
    EDGE_EXPIRY_FLOOR_SECONDS,
    EdgeTracker,
    EventTrigger,
    match_trigger,
    parse_triggers,
    serialize_trigger,
    trigger_for_topic,
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


# -- EdgeTracker (#69 CAM-R7) --------------------------------------------------


def _edge_event(
    active: bool | None,
    *,
    operation: str | None = None,
    topic_id: str = "Device/IO/VirtualInput",
    source: dict[str, str] | None = None,
) -> CameraEvent:
    return CameraEvent(
        topic_id=topic_id,
        category="io",
        source=source if source is not None else {"port": "1"},
        data={},
        active=active,
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        raw={"operation": operation} if operation else {},
    )


class TestTriggerForTopic:
    def test_returns_the_trigger_for_a_falling_edge_too(self) -> None:
        trigger = _trigger()
        assert match_trigger(_edge_event(False), [trigger]) is None
        assert trigger_for_topic(_edge_event(False), [trigger]) is trigger

    def test_disabled_and_other_topics_do_not_match(self) -> None:
        assert trigger_for_topic(_edge_event(True), [_trigger(enabled=False)]) is None
        assert trigger_for_topic(_edge_event(True), [_trigger("Other/Topic")]) is None


class TestEdgeTracker:
    @pytest.mark.parametrize(
        ("script", "expected"),
        [
            # (active, operation) per event, one second apart.
            ([(True, "Initialized")], [False]),
            ([(False, "Initialized")], [False]),
            ([(True, "Initialized"), (True, "Changed")], [False, False]),
            (
                [(True, "Initialized"), (False, "Changed"), (True, "Changed")],
                [False, False, True],
            ),
            ([(True, None), (True, None)], [True, False]),
            ([(True, None), (False, None), (True, None)], [True, False, True]),
            ([(False, None), (True, None)], [False, True]),
            ([(None, None), (None, None)], [True, True]),
            ([(None, "Initialized")], [False]),
        ],
    )
    def test_fires_only_on_a_rising_edge(
        self,
        script: list[tuple[bool | None, str | None]],
        expected: list[bool],
    ) -> None:
        tracker = EdgeTracker()
        trigger = _trigger(cooldown_seconds=0)
        fired = [
            tracker.should_fire(_edge_event(active, operation=op), trigger, float(i))
            for i, (active, op) in enumerate(script)
        ]
        assert fired == expected

    def test_sources_are_tracked_separately(self) -> None:
        tracker = EdgeTracker()
        trigger = _trigger()
        port1 = _edge_event(True, source={"port": "1"})
        port2 = _edge_event(True, source={"port": "2"})
        assert tracker.should_fire(port1, trigger, 0.0) is True
        # Port 2 asserting while port 1 is still active is its own edge.
        assert tracker.should_fire(port2, trigger, 1.0) is True
        assert tracker.should_fire(port1, trigger, 2.0) is False
        assert len(tracker) == 2

    def test_cooldown_zero_still_keeps_state_for_the_floor(self) -> None:
        tracker = EdgeTracker()
        trigger = _trigger(cooldown_seconds=0)
        assert tracker.should_fire(_edge_event(True), trigger, 0.0) is True
        just_inside = EDGE_EXPIRY_FLOOR_SECONDS - 1
        assert tracker.should_fire(_edge_event(True), trigger, just_inside) is False

    def test_a_camera_that_never_clears_triggers_again_after_expiry(self) -> None:
        tracker = EdgeTracker()
        trigger = _trigger(cooldown_seconds=100)
        assert tracker.should_fire(_edge_event(True), trigger, 0.0) is True
        assert tracker.should_fire(_edge_event(True), trigger, 399.0) is False
        # cooldown x 4 after the transition that set the entry.
        assert tracker.should_fire(_edge_event(True), trigger, 400.0) is True

    def test_repeated_state_does_not_extend_the_entry(self) -> None:
        tracker = EdgeTracker()
        trigger = _trigger(cooldown_seconds=100)
        tracker.should_fire(_edge_event(True), trigger, 0.0)
        for now in (100.0, 200.0, 300.0):
            assert tracker.should_fire(_edge_event(True), trigger, now) is False
        assert tracker.should_fire(_edge_event(True), trigger, 400.0) is True

    def test_lapsed_entries_are_pruned_on_insert(self) -> None:
        tracker = EdgeTracker()
        trigger = _trigger(cooldown_seconds=0)
        for port in range(20):
            tracker.should_fire(
                _edge_event(True, source={"port": str(port)}), trigger, 0.0
            )
        assert len(tracker) == 20
        later = EDGE_EXPIRY_FLOOR_SECONDS + 1
        tracker.should_fire(_edge_event(True, source={"port": "99"}), trigger, later)
        assert len(tracker) == 1

    def test_prefixed_topics_share_one_entry(self) -> None:
        tracker = EdgeTracker()
        trigger = _trigger()
        assert (
            tracker.should_fire(
                _edge_event(True, topic_id="tns1:Device/tnsaxis:IO/VirtualInput"),
                trigger,
                0.0,
            )
            is True
        )
        assert tracker.should_fire(_edge_event(True), trigger, 1.0) is False


class TestEdgeTrackerBounds:
    def test_a_flood_of_unique_sources_stays_within_the_caps(self) -> None:
        from timelapse_manager.capture.event_triggers import (
            EDGE_MAX_ENTRIES,
            EDGE_MAX_ENTRIES_PER_TOPIC,
        )

        tracker = EdgeTracker()
        trigger = _trigger(cooldown_seconds=10**9)
        for n in range(5000):
            tracker.should_fire(
                _edge_event(True, source={"port": str(n)}), trigger, float(n) / 100
            )
            assert len(tracker) <= EDGE_MAX_ENTRIES
        assert len(tracker) <= EDGE_MAX_ENTRIES_PER_TOPIC  # one topic

    def test_overall_cap_across_topics(self) -> None:
        from timelapse_manager.capture.event_triggers import EDGE_MAX_ENTRIES

        tracker = EdgeTracker()
        for n in range(2000):
            topic = f"Device/T{n % 100}"
            tracker.should_fire(
                _edge_event(True, topic_id=topic, source={"p": str(n)}),
                _trigger(topic),
                0.0,
            )
        assert len(tracker) <= EDGE_MAX_ENTRIES

    def test_lifetime_is_capped_whatever_the_cooldown(self) -> None:
        from timelapse_manager.capture.event_triggers import (
            EDGE_MAX_LIFETIME_SECONDS,
        )

        tracker = EdgeTracker()
        trigger = _trigger(cooldown_seconds=10**9)
        assert tracker.should_fire(_edge_event(True), trigger, 0.0) is True
        assert tracker.should_fire(_edge_event(True), trigger, 1.0) is False
        later = EDGE_MAX_LIFETIME_SECONDS
        assert tracker.should_fire(_edge_event(True), trigger, later) is True

    def test_an_oversize_source_is_bounded_and_does_not_crash(self) -> None:
        tracker = EdgeTracker()
        trigger = _trigger()
        huge = {f"k{n}" * 50: "v" * 100_000 for n in range(10_000)}
        assert tracker.should_fire(_edge_event(True, source=huge), trigger, 0.0)
        assert not tracker.should_fire(_edge_event(True, source=huge), trigger, 1.0)
        (key,) = tracker._entries  # noqa: SLF001
        assert len(key[1]) <= 8
        assert all(len(a) <= 64 and len(b) <= 64 for a, b in key[1])

    def test_evictions_are_logged_once_without_camera_text(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        tracker = EdgeTracker()
        trigger = _trigger()
        with caplog.at_level(logging.WARNING):
            for n in range(1000):
                tracker.should_fire(
                    _edge_event(True, source={"port": f"SECRET-{n}"}), trigger, 0.0
                )
        records = [r for r in caplog.records if "edge tracker evicted" in r.message]
        assert len(records) == 1
        assert "SECRET" not in caplog.text

    def test_least_recently_used_is_evicted_first(self) -> None:
        from timelapse_manager.capture.event_triggers import (
            EDGE_MAX_ENTRIES_PER_TOPIC,
        )

        tracker = EdgeTracker()
        trigger = _trigger(cooldown_seconds=100)
        first = _edge_event(True, source={"port": "first"})
        tracker.should_fire(first, trigger, 0.0)
        for n in range(EDGE_MAX_ENTRIES_PER_TOPIC - 1):
            tracker.should_fire(
                _edge_event(True, source={"port": str(n)}), trigger, 1.0
            )
        tracker.should_fire(first, trigger, 2.0)  # touched: now the newest
        tracker.should_fire(_edge_event(True, source={"port": "new"}), trigger, 3.0)
        # "first" survived, so its repeat is still not an edge.
        assert tracker.should_fire(first, trigger, 4.0) is False
