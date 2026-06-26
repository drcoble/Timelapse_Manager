"""Tests for the shared event topic helpers (the canonical key space).

The canonicaliser is the load-bearing piece: a trigger stored against a
discovery-time topic must match a live notification whose topic carries different
namespace prefixes. These tests pin the exact discovery-vs-live string triples
observed on real hardware so a regression that breaks the match is caught here.
"""

from __future__ import annotations

import pytest

from timelapse_manager.cameras import events


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Virtual input: discovery, live (inner tnsaxis: prefix), and VAPIX form
        # all collapse to one key.
        ("tns1:Device/IO/VirtualInput", "Device/IO/VirtualInput"),
        ("tns1:Device/tnsaxis:IO/VirtualInput", "Device/IO/VirtualInput"),
        ("Device/IO/VirtualInput", "Device/IO/VirtualInput"),
        # VMD profile: discovery keeps the tns1: root, live flips the whole root
        # to tnsaxis:, VAPIX is bare.
        (
            "tns1:CameraApplicationPlatform/VMD/Camera1Profile1",
            "CameraApplicationPlatform/VMD/Camera1Profile1",
        ),
        (
            "tnsaxis:CameraApplicationPlatform/VMD/Camera1Profile1",
            "CameraApplicationPlatform/VMD/Camera1Profile1",
        ),
        (
            "CameraApplicationPlatform/VMD/Camera1Profile1",
            "CameraApplicationPlatform/VMD/Camera1Profile1",
        ),
        # MotionAlarm: unchanged across discovery/live; VAPIX bare.
        ("tns1:VideoSource/MotionAlarm", "VideoSource/MotionAlarm"),
        ("VideoSource/MotionAlarm", "VideoSource/MotionAlarm"),
    ],
)
def test_canonicalize_collapses_dialects(raw: str, expected: str) -> None:
    assert events.canonicalize_topic(raw) == expected


def test_canonicalize_is_idempotent() -> None:
    once = events.canonicalize_topic("tns1:Device/tnsaxis:IO/VirtualInput")
    assert events.canonicalize_topic(once) == once


def test_canonicalize_strips_empty_and_whitespace_segments() -> None:
    assert events.canonicalize_topic("  /tns1:Device//IO/ ") == "Device/IO"


def test_canonicalize_discovery_and_live_match() -> None:
    # The whole point: the stored discovery key equals the live key.
    discovery = events.canonicalize_topic("tns1:Device/IO/VirtualInput")
    live = events.canonicalize_topic("tns1:Device/tnsaxis:IO/VirtualInput")
    assert discovery == live


@pytest.mark.parametrize(
    ("topic_id", "category"),
    [
        ("Device/IO/VirtualInput", "io"),
        ("Device/Trigger/DigitalInput", "io"),
        ("Device/Sensor/PIR", "io"),
        ("RuleEngine/MotionRegionDetector/Motion", "motion"),
        ("VideoSource/MotionAlarm", "motion"),
        ("CameraApplicationPlatform/VMD/Camera1Profile1", "motion"),
        ("CameraApplicationPlatform/ObjectAnalytics/Device1Scenario1", "analytics"),
        ("VideoSource/ImageTooBlurry/AnalyticsService", "tamper"),
        ("VideoSource/ImageTooDark/AnalyticsService", "tamper"),
        ("Device/Casing/Open", "tamper"),
        ("VideoSource/GlobalSceneChange/AnalyticsService", "scene"),
        ("CameraApplicationPlatform/camera_schedule/sunrise", "scene"),
        ("PTZController/PTZPresets/Channel_1", "other"),
        ("Device/Log/Audit", "other"),
    ],
)
def test_category_for_topic(topic_id: str, category: str) -> None:
    assert events.category_for_topic(topic_id) == category


def test_every_category_is_in_the_fixed_set() -> None:
    # The category mapping must only ever return one of the documented buckets;
    # downstream waves rely on this set verbatim.
    samples = [
        "Device/IO/VirtualInput",
        "RuleEngine/Motion",
        "CameraApplicationPlatform/ObjectAnalytics/X",
        "Device/Casing/Open",
        "VideoSource/GlobalSceneChange/AnalyticsService",
        "PTZController/X",
        "Totally/Unknown/Topic",
    ]
    for topic in samples:
        assert events.category_for_topic(topic) in events.CATEGORIES


@pytest.mark.parametrize(
    ("attrs", "stateful", "expected"),
    [
        ({"active": "1"}, True, True),
        ({"active": "0"}, True, False),
        ({"State": "1"}, True, True),
        ({"State": "0"}, True, False),
        ({"active": "true"}, True, True),
        ({"active": "false"}, True, False),
        # Stateless event: no rising-edge state regardless of fields.
        ({"active": "1"}, False, None),
        # No recognised state field: None.
        ({"port": "1"}, True, None),
        # Unrecognised LogicalState encoding: do not guess -> None.
        ({"LogicalState": "weird"}, True, None),
        ({"LogicalState": "true"}, True, True),
    ],
)
def test_normalize_active(
    attrs: dict[str, str], stateful: bool, expected: bool | None
) -> None:
    assert events.normalize_active(attrs, stateful=stateful) == expected


def test_label_humanises_camelcase_tail() -> None:
    assert events.label_for_topic("Device/IO/VirtualInput") == "Virtual Input"
    assert events.label_for_topic("VideoSource/MotionAlarm") == "Motion Alarm"


@pytest.mark.parametrize(
    ("name", "field_type"),
    [
        ("active", "boolean"),
        ("State", "boolean"),
        ("LogicalState", "boolean"),
        ("port", "string"),
        ("triggerTime", "string"),
    ],
)
def test_infer_field_type(name: str, field_type: str) -> None:
    assert events.infer_field_type(name) == field_type
