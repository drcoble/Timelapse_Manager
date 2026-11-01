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

:class:`EdgeTracker` adds the one piece of state the rules need: *which* events
are rising edges. A camera repeats a state it is already in (a heartbeat, a
re-announce after a reconnect, the ONVIF ``Initialized`` snapshot a subscription
starts with), and only the transition into "active" is an event worth a frame.
Its clock is passed in, so it stays as pure as the rest of the module.
"""

from __future__ import annotations

import logging
import uuid
from collections import OrderedDict
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


def trigger_for_topic(
    event: CameraEvent, triggers: list[EventTrigger]
) -> EventTrigger | None:
    """Return the first enabled trigger watching the event's topic, any edge.

    :func:`match_trigger` without the falling-edge drop, for the
    :class:`EdgeTracker`, which has to see the clear to know the next assertion
    is a new one.
    """
    event_topic = canonicalize_topic(event.topic_id)
    for trigger in triggers:
        if trigger.enabled and trigger.topic_id == event_topic:
            return trigger
    return None


#: The shortest an edge-tracker entry is kept, whatever the trigger's cooldown.
#: A trigger with no cooldown would otherwise forget the state at once and fire
#: on every repeat of an active state.
EDGE_EXPIRY_FLOOR_SECONDS = 60.0
#: An entry is kept this many cooldowns, so a camera that never sends the clear
#: cannot suppress the trigger for longer than that (#69 CAM-R7).
EDGE_EXPIRY_COOLDOWNS = 4

_OPERATION_INITIALIZED = "Initialized"
_EdgeKey = tuple[str, frozenset[tuple[str, str]]]

#: Hard bounds on an :class:`EdgeTracker`: a camera is untrusted, and a source
#: with endlessly new values must not grow memory (#69 CAM-R7 review).
EDGE_MAX_ENTRIES = 256
EDGE_MAX_ENTRIES_PER_TOPIC = 32
#: No entry outlives this, whatever the cooldown.
EDGE_MAX_LIFETIME_SECONDS = 600.0
#: Of an event's ``source`` items only this many (in name order) join the key,
#: each name and value cut to this many characters.
EDGE_MAX_SOURCE_ITEMS = 8
EDGE_MAX_SOURCE_TEXT = 64
#: Evictions are logged at most this often, and only as a count.
EDGE_EVICTION_LOG_INTERVAL_SECONDS = 60.0

logger = logging.getLogger(__name__)


def _bounded_source(source: dict[str, str]) -> frozenset[tuple[str, str]]:
    """Return the part of ``source`` that identifies the instance, size-bounded.

    Items are taken in name order (so the key is stable), at most
    :data:`EDGE_MAX_SOURCE_ITEMS` of them, each text cut to
    :data:`EDGE_MAX_SOURCE_TEXT` characters.
    """
    items = sorted(source.items())[:EDGE_MAX_SOURCE_ITEMS]
    return frozenset(
        (str(k)[:EDGE_MAX_SOURCE_TEXT], str(v)[:EDGE_MAX_SOURCE_TEXT]) for k, v in items
    )


def event_operation(event: CameraEvent) -> str | None:
    """Return the ONVIF ``PropertyOperation`` an event carried, if any."""
    operation = event.raw.get("operation")
    return operation if isinstance(operation, str) else None


class EdgeTracker:
    """Decides whether a live event is a rising edge worth a capture.

    State is the last value seen for each ``(topic, source)`` -- the source says
    *which* instance (port, rule) changed -- with the instant the entry lapses.
    A stateful event fires only when the value goes from not-active (or
    unknown) to active:

    * ``Initialized`` (the snapshot of current state a subscription starts with)
      records the baseline and never fires, so subscribing to a camera whose
      motion is already active does not capture;
    * ``true, true`` fires once; ``true, false, true`` fires twice;
    * a stateless event (``active is None``) is not an edge and always passes;
    * an entry lapses after :data:`EDGE_EXPIRY_COOLDOWNS` cooldowns (at least
      :data:`EDGE_EXPIRY_FLOOR_SECONDS`, at most
      :data:`EDGE_MAX_LIFETIME_SECONDS`) from the transition that set it, and a
      lapsed entry counts as unknown: a camera that never reports the clear can
      still trigger again, a bounded delay later.

    **Bounded.** The camera is untrusted, so the map is an LRU capped at
    :data:`EDGE_MAX_ENTRIES` overall and :data:`EDGE_MAX_ENTRIES_PER_TOPIC` per
    topic, and the source part of the key is cut down (see
    :func:`_bounded_source`). An evicted entry is simply unknown again, so the
    worst a flood does is let a still-active state fire once more. Evictions are
    logged as a count, rarely, never with camera text.

    One tracker belongs to one listener and outlives its subscriptions, so a
    camera that re-sends its state after a reconnect (the VAPIX WebSocket has no
    ``Initialized`` marker) is not mistaken for a new edge.
    """

    def __init__(self) -> None:
        self._entries: OrderedDict[_EdgeKey, tuple[bool, float]] = OrderedDict()
        self._evicted = 0
        self._last_logged: float | None = None

    def __len__(self) -> int:
        """Return the number of tracked ``(topic, source)`` entries."""
        return len(self._entries)

    def should_fire(
        self, event: CameraEvent, trigger: EventTrigger, now: float
    ) -> bool:
        """Record ``event`` and return whether it is a rising edge to capture.

        :param now: a monotonic reading in seconds.
        """
        if event.active is None:
            return event_operation(event) != _OPERATION_INITIALIZED
        key = (canonicalize_topic(event.topic_id), _bounded_source(event.source))
        previous = self._entries.get(key)
        previous_active = (
            previous[0] if previous is not None and now < previous[1] else None
        )
        if previous_active != event.active:
            self._prune(now)
            expiry = min(
                max(
                    trigger.cooldown_seconds * EDGE_EXPIRY_COOLDOWNS,
                    EDGE_EXPIRY_FLOOR_SECONDS,
                ),
                EDGE_MAX_LIFETIME_SECONDS,
            )
            self._entries[key] = (event.active, now + expiry)
            self._entries.move_to_end(key)
            self._enforce_caps(key[0], now)
        elif previous is not None:
            self._entries.move_to_end(key)
        if event_operation(event) == _OPERATION_INITIALIZED:
            return False
        return event.active is True and previous_active is not True

    def _prune(self, now: float) -> None:
        """Drop every lapsed entry."""
        for key in [k for k, (_, lapses) in self._entries.items() if now >= lapses]:
            del self._entries[key]

    def _enforce_caps(self, topic: str, now: float) -> None:
        """Evict least-recently-used entries past the per-topic and overall caps."""
        topic_keys = [k for k in self._entries if k[0] == topic]
        while len(topic_keys) > EDGE_MAX_ENTRIES_PER_TOPIC:
            del self._entries[topic_keys.pop(0)]
            self._evicted += 1
        while len(self._entries) > EDGE_MAX_ENTRIES:
            self._entries.popitem(last=False)
            self._evicted += 1
        self._log_evictions(now)

    def _log_evictions(self, now: float) -> None:
        """Log the eviction count, at most once per interval; no camera text."""
        if not self._evicted:
            return
        last = self._last_logged
        if last is not None and now - last < EDGE_EVICTION_LOG_INTERVAL_SECONDS:
            return
        logger.warning(
            "edge tracker evicted %d entr%s to stay within its bounds; the camera "
            "is sending an unusual number of distinct event sources",
            self._evicted,
            "y" if self._evicted == 1 else "ies",
        )
        self._evicted = 0
        self._last_logged = now
