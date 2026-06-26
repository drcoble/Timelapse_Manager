"""Parsing and matching for a project's stored event triggers.

A project opts a camera event into capture by storing a small trigger record:
the canonical topic to watch, a human label, its category, an enabled flag, and a
per-trigger cooldown. The supervisor reads that stored list, turns it into typed
:class:`EventTrigger` objects here, and asks :func:`match_trigger` which (if any)
trigger a live :class:`~timelapse_manager.cameras.base.CameraEvent` satisfies.

Everything in this module is pure: no time, no I/O, no protocol state. The
debounce clock and the capture call live on the supervisor; the rules for *what
counts as a match* live here so they can be unit-tested in isolation.

The match rules are deliberately small:

* topics are compared on their canonical (prefix-stripped) form, so a live
  notification whose vendor prefixes differ from the stored descriptor still
  matches -- the same normalisation :func:`canonicalize_topic` applies at
  discovery time is applied defensively to both sides here;
* only an *enabled* trigger can fire;
* a stateful event fires only on its rising edge (``active is True``); a clear /
  falling edge (``active is False``) never fires; a stateless event
  (``active is None``) fires.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..cameras.events import canonicalize_topic

if TYPE_CHECKING:
    from ..cameras.base import CameraEvent


@dataclass(frozen=True)
class EventTrigger:
    """One configured event-to-capture rule, parsed from the stored record.

    :param id: a stable identifier for the trigger (used as provenance on the
        captured frame's audit metadata). Generated when the stored record omits
        it.
    :param topic_id: the canonical topic this trigger watches, compared against a
        live event's canonical ``topic_id``.
    :param label: the human-facing name of the trigger.
    :param category: the coarse grouping the topic falls under (carried through
        for display and provenance; not used for matching).
    :param enabled: whether the trigger is live -- a disabled trigger never fires.
    :param cooldown_seconds: the minimum gap, in seconds, between two captures
        this trigger may cause. ``0`` means no debounce. Never negative.
    """

    id: str
    topic_id: str
    label: str
    category: str
    enabled: bool
    cooldown_seconds: int


def parse_triggers(stored: Any) -> list[EventTrigger]:
    """Parse a project's stored ``event_triggers`` list into typed triggers.

    ``stored`` is the value persisted on the project (a list of plain dicts) or
    ``None`` when the project configured none. Each record is validated and
    normalised:

    * a missing or blank ``id`` is replaced by a fresh ``uuid4`` hex string;
    * ``topic_id`` is canonicalised defensively (so a record stored with a
      vendor prefix still matches live events), and a blank topic is rejected;
    * ``enabled`` defaults to ``True`` when absent;
    * ``cooldown_seconds`` defaults to ``0`` and must be a non-negative integer.

    :raises ValueError: naming the offending field when a record is not a
        mapping, the topic is blank, or the cooldown is not a non-negative
        integer.
    """
    if not stored:
        return []
    if not isinstance(stored, list):
        raise ValueError("event_triggers must be a list")

    triggers: list[EventTrigger] = []
    for index, record in enumerate(stored):
        if not isinstance(record, dict):
            raise ValueError(f"event_triggers[{index}] must be a mapping")
        triggers.append(_parse_one(record, index))
    return triggers


def _parse_one(record: dict[str, Any], index: int) -> EventTrigger:
    raw_topic = record.get("topic_id")
    if not isinstance(raw_topic, str) or not raw_topic.strip():
        raise ValueError(f"event_triggers[{index}].topic_id is required")
    topic_id = canonicalize_topic(raw_topic)
    if not topic_id:
        raise ValueError(f"event_triggers[{index}].topic_id is empty after parsing")

    raw_id = record.get("id")
    trigger_id = raw_id.strip() if isinstance(raw_id, str) and raw_id.strip() else None
    if trigger_id is None:
        trigger_id = uuid.uuid4().hex

    label = record.get("label")
    label = label if isinstance(label, str) else ""
    category = record.get("category")
    category = category if isinstance(category, str) else ""

    enabled = record.get("enabled", True)
    enabled = bool(enabled)

    cooldown = record.get("cooldown_seconds", 0)
    if isinstance(cooldown, bool) or not isinstance(cooldown, int) or cooldown < 0:
        raise ValueError(
            f"event_triggers[{index}].cooldown_seconds must be a non-negative integer"
        )

    return EventTrigger(
        id=trigger_id,
        topic_id=topic_id,
        label=label,
        category=category,
        enabled=enabled,
        cooldown_seconds=cooldown,
    )


def serialize_trigger(trigger: EventTrigger) -> dict[str, Any]:
    """Return the stored-record dict form of a trigger (symmetry with parsing)."""
    return {
        "id": trigger.id,
        "topic_id": trigger.topic_id,
        "label": trigger.label,
        "category": trigger.category,
        "enabled": trigger.enabled,
        "cooldown_seconds": trigger.cooldown_seconds,
    }


def match_trigger(
    event: CameraEvent, triggers: list[EventTrigger]
) -> EventTrigger | None:
    """Return the first enabled trigger a live event satisfies, or ``None``.

    Pure: no time, no I/O. A falling edge never fires, so an event whose
    normalised state is ``False`` returns ``None`` before any trigger is
    examined. Otherwise the first *enabled* trigger whose canonical ``topic_id``
    equals the event's canonical ``topic_id`` wins; if none match, ``None``.

    Both topic ids are canonicalised here defensively so a match holds even if a
    trigger was stored, or an event arrives, with a vendor-prefixed topic.
    """
    if event.active is False:
        # A clear / falling edge is never a capture trigger -- only a rising edge
        # (True) or a stateless event (None) fires.
        return None

    event_topic = canonicalize_topic(event.topic_id)
    for trigger in triggers:
        if trigger.enabled and trigger.topic_id == event_topic:
            return trigger
    return None
