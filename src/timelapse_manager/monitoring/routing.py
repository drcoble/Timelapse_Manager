"""Pure routing logic: map an event to the set of channels it should reach.

A routing rule is a plain dict (as persisted in the notification settings):

.. code-block:: python

    {
        "event_types": ["camera.offline_threshold", "render.failed"],  # or ["all"]
        "min_level": "warning",
        "channels": ["email", "webhook"],
    }

:func:`evaluate_routing_rules` is deliberately a pure function over the event's
``(type, level)`` and a list of such rules: it performs no I/O, reads no global
state, and is trivially testable. An empty result means "record in the
application only, do not notify any channel".

The notification delivery-failure event type is hard-excluded here so that a
failed delivery can never itself be routed to a channel -- the single chokepoint
that prevents a notification storm when a channel is unhealthy.
"""

from __future__ import annotations

from typing import Any

from .events import EventType, _levels_at_or_above

# The wildcard token a rule may use in place of an explicit event-type list.
_ALL = "all"

# Event types that must never be routed to a channel, regardless of any rule.
# A delivery failure is recorded in-application only; routing it would risk a
# feedback loop where a failing channel keeps generating fresh notifications.
_NEVER_ROUTED: frozenset[str] = frozenset({EventType.NOTIFY_DELIVERY_FAILED.value})


def evaluate_routing_rules(
    event_type: str,
    level: str,
    routing_rules: list[dict[str, Any]],
) -> set[str]:
    """Return the set of channel names an event should be delivered to.

    A rule matches when both conditions hold:

    * **type** -- the rule's ``event_types`` contains ``event_type`` or the
      wildcard ``"all"``.
    * **level** -- ``level`` is at or above the rule's ``min_level``. A rule with
      no ``min_level`` (or an unrecognised one) imposes no level floor.

    The channels of every matching rule are unioned. An empty set means the
    event is recorded in-application only.

    :param event_type: the event's dotted type identifier.
    :param level: the event's severity name.
    :param routing_rules: the configured rules; a non-list or malformed entries
        are tolerated and simply do not match.
    :returns: the set of channel names to deliver to (possibly empty).
    """
    if event_type in _NEVER_ROUTED:
        return set()
    if not isinstance(routing_rules, list):
        return set()

    matched: set[str] = set()
    for rule in routing_rules:
        if not isinstance(rule, dict):
            continue
        if not _type_matches(event_type, rule.get("event_types")):
            continue
        if not _level_meets(level, rule.get("min_level")):
            continue
        channels = rule.get("channels")
        if isinstance(channels, list):
            matched.update(str(c) for c in channels)
    return matched


def _type_matches(event_type: str, event_types: Any) -> bool:
    """Return whether ``event_type`` satisfies a rule's ``event_types`` list."""
    if not isinstance(event_types, list):
        return False
    if _ALL in event_types:
        return True
    return event_type in event_types


def _level_meets(level: str, min_level: Any) -> bool:
    """Return whether ``level`` is at or above a rule's ``min_level``.

    A missing or unrecognised ``min_level`` imposes no floor (the rule matches
    on every severity), consistent with the level-floor query semantics.
    """
    if not isinstance(min_level, str):
        return True
    return level in _levels_at_or_above(min_level)
