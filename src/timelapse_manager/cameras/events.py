"""Shared helpers for camera event topics and notifications.

Two camera protocols (ONVIF and Axis VAPIX) describe and emit the same physical
events with subtly different topic strings, so a single canonical key space is
needed to make discovery-time descriptors and runtime notifications line up.

The crux is :func:`canonicalize_topic`. A topic the camera advertises at
discovery time is *not* byte-identical to the topic it puts on a live
notification: the live string can sprout a vendor namespace prefix on an inner
path segment (``Device/IO/...`` becomes ``Device/tnsaxis:IO/...``) or flip the
root prefix entirely (``tns1:Camera...`` becomes ``tnsaxis:Camera...``). A naive
``==`` match against the stored discovery string therefore silently never fires
for the prefix-shifted topics. Canonicalizing every topic to a prefix-stripped,
``/``-joined path collapses all three dialects (ONVIF discovery, ONVIF live,
VAPIX) onto one key, which is exactly the form VAPIX already returns.

Everything here is pure: no I/O, no network, no protocol state.
"""

from __future__ import annotations

# The fixed set of categories an event descriptor is grouped under, mapped from
# the leading canonical topic segment. Kept small and UI-facing on purpose; any
# topic that does not match a known group falls through to ``other``.
CATEGORIES = ("motion", "tamper", "analytics", "io", "scene", "other")

# Field values a stateful event uses to mean "asserted" (rising edge). Compared
# case-folded against the flattened source+data attributes. The boolean-state
# field names a stateful topic carries vary by topic (``active`` for IO/property
# topics, ``State`` for the motion/scene state topics); both are checked.
_STATE_FIELDS = ("active", "State", "state")
_TRUE_STATE_VALUES = frozenset({"1", "true"})
_FALSE_STATE_VALUES = frozenset({"0", "false"})


def canonicalize_topic(raw_topic: str) -> str:
    """Return the prefix-stripped, ``/``-joined canonical form of a topic.

    Splits on ``/`` and drops any ``prefix:`` namespace qualifier from each
    segment, then re-joins. This collapses the per-dialect prefix differences so
    ONVIF discovery (``tns1:Device/IO/VirtualInput``), ONVIF live
    (``tns1:Device/tnsaxis:IO/VirtualInput``), and VAPIX
    (``Device/IO/VirtualInput``) all yield the same key
    (``Device/IO/VirtualInput``).

    Empty segments (a stray leading/trailing or doubled ``/``) are dropped so the
    key is stable. A topic that is already canonical is returned unchanged.
    """
    segments: list[str] = []
    for segment in raw_topic.strip().split("/"):
        # Drop a leading ``prefix:`` qualifier on the segment. ``rpartition``
        # keeps a segment with no colon intact and strips only the namespace part
        # of a qualified one, never touching the local name.
        local = segment.rpartition(":")[2].strip()
        if local:
            segments.append(local)
    return "/".join(segments)


def category_for_topic(topic_id: str) -> str:
    """Map a canonical ``topic_id`` to one of :data:`CATEGORIES`.

    The mapping keys off the canonical path so it is protocol-agnostic. Motion,
    tamper, analytics, io, and scene are recognised by their leading/whole
    segments; everything else (device health, PTZ, audit logs, unknown vendor
    topics) collapses to ``other``.
    """
    first = topic_id.split("/", 1)[0]
    # Analytics (often ACAP-backed) before the broader buckets so an
    # ObjectAnalytics topic is not mistaken for a plain platform topic.
    if "ObjectAnalytics" in topic_id or "ImageHealthAnalytics" in topic_id:
        return "analytics"
    if "VMD" in topic_id or first == "RuleEngine" or topic_id.endswith("MotionAlarm"):
        return "motion"
    if "ImageTooBlurry" in topic_id or "ImageTooDark" in topic_id:
        return "tamper"
    if topic_id.startswith("Device/Casing"):
        return "tamper"
    if "GlobalSceneChange" in topic_id or "camera_schedule" in topic_id:
        return "scene"
    segments = topic_id.split("/")
    if (
        first == "Device"
        and len(segments) > 1
        and segments[1]
        in (
            "IO",
            "Trigger",
            "Sensor",
        )
    ):
        return "io"
    return "other"


def label_for_topic(topic_id: str) -> str:
    """Build a human-readable label from a canonical ``topic_id``.

    Best-effort and purely cosmetic: the last meaningful path segment is split on
    camel-case boundaries into spaced words, so ``Device/IO/VirtualInput`` reads
    as "Virtual Input". Falls back to the whole topic when there is nothing to
    humanise.
    """
    segments = [s for s in topic_id.split("/") if s]
    if not segments:
        return topic_id
    tail = segments[-1]
    words: list[str] = []
    current = ""
    for char in tail:
        if char.isupper() and current and not current[-1].isupper():
            words.append(current)
            current = char
        else:
            current += char
    if current:
        words.append(current)
    return " ".join(words) if words else tail


def normalize_active(attrs: dict[str, str], *, stateful: bool) -> bool | None:
    """Resolve the rising-edge state of an event from its flattened attributes.

    ``attrs`` is the merged ``source`` + ``data`` name->value map (all strings,
    as both protocols emit). For a stateful topic, the recognised boolean-state
    field (``active``/``State``) is read: ``"1"``/``"true"`` is the asserted
    (rising) edge -> True, ``"0"``/``"false"`` is the clear (falling) edge ->
    False. A stateless topic, or one with no recognised state field, yields
    ``None`` ("not a state transition").

    ``LogicalState`` (digital-input) is treated as a state field too, but its
    real on-the-wire encoding was not confirmed live, so only the same
    ``"1"``/``"true"`` truthy set is honoured; an unrecognised value reads as
    ``None`` rather than guessing.
    """
    if not stateful:
        return None
    for field in (*_STATE_FIELDS, "LogicalState"):
        if field in attrs:
            value = attrs[field].strip().lower()
            if value in _TRUE_STATE_VALUES:
                return True
            if value in _FALSE_STATE_VALUES:
                return False
            return None
    return None


def infer_field_type(name: str) -> str:
    """Infer a coarse value type for a source/data field from its name.

    Names that denote a boolean state (``active``, ``State``, ``running``,
    ``ready``, ``LogicalState``) are typed ``"boolean"``; everything else
    defaults to ``"string"``. This mirrors how the camera reports values (all as
    strings) while giving a UI a hint for rendering and for defaulting a
    rising-edge predicate.
    """
    lowered = name.strip().lower()
    if lowered in ("active", "state", "running", "ready", "logicalstate"):
        return "boolean"
    return "string"
