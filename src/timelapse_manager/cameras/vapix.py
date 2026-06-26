"""Axis VAPIX adapter.

Axis cameras expose a snapshot CGI at ``/axis-cgi/jpg/image.cgi`` that accepts
query parameters such as ``resolution`` and ``compression``. Capture and the
auth handling reuse the generic HTTP/JPEG helpers; this adapter only adds the
Axis-specific URL construction, a capability/geolocation query against the Axis
parameter CGI, and resolution/compression knobs.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit
from xml.etree import ElementTree as ET

import httpx

from ..security.ssrf import SsrfError, assert_allowed_url
from . import events as eventutil
from .base import (
    CameraAdapter,
    CameraCapabilities,
    CameraEvent,
    CapturedFrame,
    CaptureError,
    EventDescriptor,
    EventNotSupportedError,
    GeoLocation,
    PTZError,
    PTZPreset,
    PTZPresetsResult,
    StreamProfile,
    StreamProfileResult,
    UnreachableCaptureError,
    ValidationResult,
)
from .http_jpeg import frame_from_bytes, http_get_image

logger = logging.getLogger(__name__)

SNAPSHOT_PATH = "/axis-cgi/jpg/image.cgi"
PARAM_PATH = "/axis-cgi/param.cgi"
PTZ_PATH = "/axis-cgi/com/ptz.cgi"
# The SOAP endpoint and namespace for the VAPIX event-instances query, which
# enumerates the device's event topics with their Source/Data field names and an
# ``isProperty`` (stateful) marker. Works over plain HTTP digest on every Axis
# firmware, including older devices where the ONVIF account is not provisioned.
VAPIX_SERVICES_PATH = "/vapix/services"
VAPIX_EVENT_NS = "http://www.axis.com/vapix/ws/event1"
# The WebSocket event stream (optional fast-path). Present on newer firmware;
# older devices answer 404, which degrades gracefully to "no WS events".
WS_DATA_STREAM_PATH = "/vapix/ws-data-stream?sources=events"
# The dedicated Geolocation API on current Axis firmware. It returns an XML
# position document and is the authoritative source; the legacy
# ``param.cgi?group=Geolocation`` does not exist on these devices (it answers
# with an error), so the API is queried first and the param group is only a
# fallback for older cameras.
GEOLOCATION_PATH = "/axis-cgi/geolocation/get.cgi"

# Axis PTZ commands address a specific camera channel; channel 1 is the sole
# channel on a single-head PTZ dome and the correct default for the rest.
PTZ_CAMERA = "1"

# How long to wait after a positioning command so the physical dome has settled
# on the new position before the caller captures from it. A bounded pause that
# trades a few seconds of latency for a frame taken at the intended position
# rather than mid-travel.
PTZ_SETTLE_SECONDS = 3.0

# The Axis parameter group whose ``*.PTZEnabled`` flag reports whether the device
# has a working pan/tilt/zoom mechanism. Read alongside the preset list to decide
# whether to advertise PTZ support for a camera that happens to define no presets.
PTZ_PARAM_GROUP = "PTZ"

# Parameter values Axis uses for a true boolean. Compared case-folded; anything
# else (including non-empty negatives like ``"no"``/``"false"``) is treated as
# False, so a disabled flag is never mistaken for an enabled one.
_TRUTHY_PARAM_VALUES = frozenset({"yes", "true", "1", "on", "enabled"})

# Axis VAPIX compression is a 0..100 scale (0 = least compression / best image).
COMPRESSION_RANGE = (0, 100)

# The Axis parameter group that exposes per-channel scene/image settings, e.g.
# ``root.Image.I0.Appearance.Brightness``, ``...Appearance.Contrast``,
# ``...Appearance.Saturation``, ``...Appearance.Sharpness``, and
# ``root.Image.I0.Exposure.ExposureValue`` / ``...Exposure.ExposurePriority``.
# These are the camera's effective scene settings -- cheap to read in a single
# param query -- which the capture path snapshots alongside each frame.
SCENE_PARAM_GROUP = "Image"

# Version of the scene-metadata envelope persisted with each frame. Bump only
# when the envelope's shape changes in a way readers must account for.
SCENE_METADATA_SCHEMA_VERSION = 1

# A short timeout for the best-effort scene-metadata read on the capture hot
# path. Kept well under any reasonable per-capture budget so a slow or
# unresponsive parameter CGI degrades to "no metadata" quickly rather than
# delaying (or, under the supervisor's outer wait_for, cancelling) the capture.
SCENE_METADATA_TIMEOUT = 2.0

# The scene-setting fields lifted out of the Image group into the envelope, keyed
# by the envelope field name and matched on the parameter key suffix
# (case-folded). Each is included only when the camera actually reports it, so an
# envelope never invents values the device did not expose.
_SCENE_FIELDS: tuple[tuple[str, str], ...] = (
    # Appearance settings every Axis source exposes (resolution, compression,
    # rotation, overlay mode). These are present across firmware generations, so
    # the envelope carries real scene state even on devices that omit the finer
    # image-tuning fields below.
    ("appearance_resolution", "Appearance.Resolution"),
    ("compression", "Appearance.Compression"),
    ("rotation", "Appearance.Rotation"),
    ("overlays", "Appearance.Overlays"),
    # Finer image-tuning fields, present on some firmware generations only. Each
    # is included only when the camera actually reports it, so an envelope never
    # invents values the device did not expose.
    ("brightness", "Appearance.Brightness"),
    ("contrast", "Appearance.Contrast"),
    ("saturation", "Appearance.Saturation"),
    ("sharpness", "Appearance.Sharpness"),
    ("color_enabled", "Appearance.ColorEnabled"),
    ("exposure_value", "Exposure.ExposureValue"),
    ("exposure_priority", "Exposure.ExposurePriority"),
)

# The Axis parameter group that carries the device's network identity. Its
# ``HostName`` key holds the configured hostname; ``Network.HostName`` is the
# canonical key, though the param CGI may emit it with or without the ``root.``
# prefix. Some firmware also surfaces a derived FQDN under
# ``Network.DNSServerAddress``-adjacent keys, but the plain ``HostName`` is the
# stable, widely-present value we read.
NETWORK_PARAM_GROUP = "Network"

# The Axis parameter group that lists named stream profiles. Each profile is a
# numbered sub-group whose keys end in ``.Name`` (display name) and
# ``.Parameters`` (a ``&``-joined query fragment of the stream's encoder
# settings, e.g. ``resolution=1280x720&compression=30&...``). The profile's
# stable identifier is its ``Name`` -- that is the token a caller stores and
# hands back to select the profile on a later capture.
STREAM_PROFILE_GROUP = "StreamProfile"


def _base_url(address: str) -> str:
    """Return the http base URL for an address, defaulting the scheme to http.

    Any userinfo (``user:pass@``) the operator may have embedded in the address
    is dropped: VAPIX authentication is sent in request headers, never in the
    URL, and the composed URL is surfaced/persisted as the snapshot URI, so it
    must never carry credentials.
    """
    if address.startswith(("http://", "https://")):
        split = urlsplit(address)
        host = split.hostname or ""
        if ":" in host:  # IPv6 literal -- restore the brackets a netloc needs.
            host = f"[{host}]"
        netloc = f"{host}:{split.port}" if split.port else host
        return f"{split.scheme}://{netloc}"
    return f"http://{address}".rstrip("/")


def build_snapshot_url(
    address: str,
    resolution: str | None = None,
    compression: int | None = None,
    explicit_snapshot_uri: str | None = None,
) -> str:
    """Build the VAPIX snapshot URL, optionally with resolution/compression.

    If an explicit snapshot URI is configured it is used verbatim (the operator
    knows best); otherwise the standard Axis CGI path is composed from the
    address and the optional query parameters.
    """
    if explicit_snapshot_uri:
        return explicit_snapshot_uri
    params: dict[str, str] = {}
    if resolution:
        params["resolution"] = resolution
    if compression is not None:
        params["compression"] = str(compression)
    url = f"{_base_url(address)}{SNAPSHOT_PATH}"
    if params:
        url = f"{url}?{urlencode(params)}"
    return url


def parse_param_response(text: str) -> dict[str, str]:
    """Parse an Axis ``param.cgi`` ``key=value`` response into a dict."""
    result: dict[str, str] = {}
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key.strip()] = value.strip()
    return result


def parse_stream_profiles(params: dict[str, str]) -> list[StreamProfile]:
    """Extract named stream profiles from a ``StreamProfile`` param group.

    Axis exposes each profile as a numbered sub-group, e.g.::

        root.StreamProfile.S0.Name=Quality
        root.StreamProfile.S0.Parameters=resolution=1920x1080&compression=10
        root.StreamProfile.S1.Name=Bandwidth
        root.StreamProfile.S1.Parameters=resolution=640x480&compression=40

    The profile's ``Name`` is used as both the stable :attr:`StreamProfile.id`
    (the token round-tripped to select it later) and its display label. Profiles
    are returned in numeric sub-group order; an entry missing a name is skipped.
    """
    names: dict[int, str] = {}
    for key, value in params.items():
        index = _stream_profile_index(key)
        if index is not None and key.endswith(".Name") and value:
            names[index] = value
    return [StreamProfile(id=names[i], label=names[i]) for i in sorted(names)]


def stream_profile_parameters(params: dict[str, str], profile_id: str) -> str | None:
    """Return the raw ``Parameters`` fragment for the profile named ``profile_id``.

    The match is on the profile's ``Name`` (its id). Returns None when no profile
    with that name is present, or it has no ``Parameters`` value.
    """
    target_index: int | None = None
    for key, value in params.items():
        index = _stream_profile_index(key)
        if index is not None and key.endswith(".Name") and value == profile_id:
            target_index = index
            break
    if target_index is None:
        return None
    for key, value in params.items():
        index = _stream_profile_index(key)
        if index == target_index and key.endswith(".Parameters"):
            return value or None
    return None


def snapshot_knobs_from_parameters(
    parameters: str,
) -> tuple[str | None, int | None]:
    """Pull only the snapshot-relevant knobs from a profile ``Parameters`` value.

    A profile's ``Parameters`` is a ``&``-joined query fragment of mixed stream
    settings (codec, frame rate, audio, resolution, ...). Only the two that the
    snapshot CGI honours -- ``resolution`` and ``compression`` -- are extracted;
    everything else is ignored so unrelated stream settings are never splattered
    onto the still-image request. Returns ``(resolution, compression)`` with
    either element None when that knob is absent or unparseable.
    """
    fields = dict(parse_qsl(parameters, keep_blank_values=False))
    resolution = fields.get("resolution") or None
    compression: int | None = None
    raw_compression = fields.get("compression")
    if raw_compression is not None:
        try:
            compression = int(raw_compression)
        except ValueError:
            compression = None
    return resolution, compression


def _stream_profile_index(key: str) -> int | None:
    """Return the numeric sub-group index of a ``StreamProfile.S<n>.*`` key.

    Tolerates an optional ``root.`` prefix (Axis emits keys both ways). Returns
    None for any key that is not a numbered stream-profile entry.
    """
    parts = key.split(".")
    for i, part in enumerate(parts[:-1]):
        if part == STREAM_PROFILE_GROUP:
            token = parts[i + 1]
            if token.startswith("S") and token[1:].isdigit():
                return int(token[1:])
            return None
    return None


def geolocation_from_params(params: dict[str, str]) -> GeoLocation | None:
    """Extract a :class:`GeoLocation` from Axis parameter values, if present.

    Axis devices may expose ``Geolocation.Latitude`` / ``Geolocation.Longitude``
    (also seen as ``root.Geolocation.*``). Returns None when absent or unpar-
    seable.
    """
    latitude = _find_param(params, "Latitude")
    longitude = _find_param(params, "Longitude")
    if latitude is None or longitude is None:
        return None
    try:
        return GeoLocation(
            latitude=float(latitude),
            longitude=float(longitude),
            source="camera",
        )
    except ValueError:
        return None


def geolocation_from_position_xml(text: str) -> GeoLocation | None:
    """Extract a :class:`GeoLocation` from the Axis Geolocation API response.

    The ``geolocation/get.cgi`` endpoint returns a position document such as::

        <PositionResponse SchemaVersion="1.0">
          <Success><GetSuccess>
            <Location><Lat>34.12</Lat><Lng>-83.93</Lng><Heading>45.0</Heading></Location>
            <ValidPosition>true</ValidPosition>
          </GetSuccess></Success>
        </PositionResponse>

    Returns the parsed latitude/longitude (source ``"camera"``), or None when the
    document is an error envelope, carries no ``Location``, explicitly reports
    ``ValidPosition`` false, or cannot be parsed. Heading is not modelled and is
    ignored.
    """
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return None
    location = root.find(".//Location")
    if location is None:
        return None
    valid = root.findtext(".//ValidPosition")
    if valid is not None and valid.strip().lower() not in _TRUTHY_PARAM_VALUES:
        return None
    latitude = location.findtext("Lat")
    longitude = location.findtext("Lng")
    if latitude is None or longitude is None:
        return None
    try:
        return GeoLocation(
            latitude=float(latitude),
            longitude=float(longitude),
            source="camera",
        )
    except ValueError:
        return None


def hostname_from_params(params: dict[str, str]) -> str | None:
    """Extract the device hostname from a ``Network`` param group, if present.

    Axis exposes the configured hostname as ``root.Network.HostName`` (also seen
    without the ``root.`` prefix). The match is on the ``HostName`` key suffix so
    either form works, and a derived ``*.HostName`` from a nested sub-group is
    avoided by requiring the key to end in exactly ``Network.HostName``.

    Returns the trimmed hostname, or None when the group exposes no hostname, the
    value is empty, or it is the Axis placeholder a never-configured device emits.
    """
    raw = _find_network_hostname(params)
    if raw is None:
        return None
    hostname = raw.strip()
    if not hostname:
        return None
    # Axis emits a literal placeholder when no hostname has been configured;
    # treat it as "no hostname" rather than reporting the placeholder verbatim.
    if hostname.lower() in _HOSTNAME_PLACEHOLDERS:
        return None
    return hostname


# Values an unconfigured Axis device returns for the hostname; compared
# case-folded. These are not real hostnames, so they read as "no hostname".
_HOSTNAME_PLACEHOLDERS = frozenset({"<hostname>", "set hostname", "axis"})


def _find_network_hostname(params: dict[str, str]) -> str | None:
    """Return the value of the ``Network.HostName`` key, prefix-tolerant."""
    for key, value in params.items():
        normalised = key.lower().removeprefix("root.")
        if normalised == "network.hostname" and value:
            return value
    return None


def _find_param(params: dict[str, str], suffix: str) -> str | None:
    """Return the first param whose key ends with ``suffix`` (case-folded)."""
    target = suffix.lower()
    for key, value in params.items():
        if key.lower().endswith(target) and value:
            return value
    return None


def scene_fields_from_params(params: dict[str, str]) -> dict[str, str]:
    """Extract the scene-setting fields from an ``Image`` param group.

    Returns a dict keyed by the envelope field name (``brightness``,
    ``contrast``, ...) holding the camera's reported value as a string. Only
    fields the camera actually exposed are present; an absent or empty value is
    skipped rather than invented. Values are kept verbatim as the camera returned
    them so no interpretation or unit assumption is baked in. Returns an empty
    dict when the group is empty or exposes none of the known fields.
    """
    fields: dict[str, str] = {}
    for envelope_key, param_suffix in _SCENE_FIELDS:
        value = _find_param(params, param_suffix)
        if value is not None:
            fields[envelope_key] = value
    return fields


def parse_ptz_presets(text: str) -> list[PTZPreset]:
    """Extract named PTZ presets from a ``query=presetposall`` response.

    Axis returns plain text with a leading header line and one entry per preset::

        Preset Positions for camera 1
        presetposno1=Home
        presetposno2=Position 1
        presetposno3=position 2

    Each ``presetposno<N>=<Name>`` line yields a preset whose id and label are
    both the ``Name`` -- the name is the token the goto command expects, and the
    only stable handle Axis offers. The header line has no ``=`` and is dropped.
    Presets are returned in ascending numeric (``<N>``) order; an entry with an
    empty name is skipped.
    """
    numbered: dict[int, str] = {}
    for key, value in parse_param_response(text).items():
        index = _preset_index(key)
        if index is not None and value:
            numbered[index] = value
    return [PTZPreset(id=numbered[i], label=numbered[i]) for i in sorted(numbered)]


def _preset_index(key: str) -> int | None:
    """Return the numeric suffix of a ``presetposno<N>`` key, else None."""
    prefix = "presetposno"
    if key.startswith(prefix) and key[len(prefix) :].isdigit():
        return int(key[len(prefix) :])
    return None


def ptz_enabled_from_params(params: dict[str, str]) -> bool:
    """Return whether a ``PTZ`` param group reports a working PTZ mechanism.

    Reads the group's ``*.PTZEnabled`` flag and interprets it as a boolean. Axis
    returns the flag as a non-empty string either way (``"yes"``/``"no"``), so a
    mere presence check is not enough -- the value itself must be truthy. An
    absent flag reads as False.
    """
    raw = _find_param(params, "PTZEnabled")
    return raw is not None and raw.strip().lower() in _TRUTHY_PARAM_VALUES


def is_ptz_error_body(text: str) -> bool:
    """Return whether a VAPIX response body signals a failure.

    Axis answers a rejected PTZ command with a ``2xx`` status and an ``Error:``
    text body (also seen as ``# Error:``) rather than an HTTP error code, so the
    body must be inspected. Matched case-insensitively on the stripped body, which
    covers both forms a successful command never produces.
    """
    normalised = text.strip().lower()
    return normalised.startswith("error") or normalised.startswith("# error")


def build_ptz_goto_url(address: str, preset_id: str) -> str:
    """Build the VAPIX URL that recalls a named preset position."""
    query = urlencode({"camera": PTZ_CAMERA, "gotoserverpresetname": preset_id})
    return f"{_base_url(address)}{PTZ_PATH}?{query}"


def build_ptz_move_url(
    address: str,
    pan: float | None = None,
    tilt: float | None = None,
    zoom: float | None = None,
) -> str:
    """Build the VAPIX URL for a raw absolute pan/tilt/zoom move.

    Only the axes that are not None are included, so a caller can move one axis
    without disturbing the others. Values are passed through verbatim -- the
    camera clamps them to its own ranges (this dome's are roughly pan -180..180,
    tilt -90..0, zoom 1..9999 in camera units), so no scaling or 0..1 assumption
    is imposed here.
    """
    params: dict[str, str] = {"camera": PTZ_CAMERA}
    if pan is not None:
        params["pan"] = str(pan)
    if tilt is not None:
        params["tilt"] = str(tilt)
    if zoom is not None:
        params["zoom"] = str(zoom)
    return f"{_base_url(address)}{PTZ_PATH}?{urlencode(params)}"


def get_event_instances_body() -> str:
    """Build the SOAP request that enumerates the device's event instances."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope">'
        "<soap:Body>"
        f'<GetEventInstances xmlns="{VAPIX_EVENT_NS}"/>'
        "</soap:Body></soap:Envelope>"
    )


def parse_event_instances(xml_text: str) -> list[EventDescriptor]:
    """Parse a GetEventInstances response into canonical event descriptors.

    Axis returns a nested ``TopicSet`` tree where the path to each leaf is the
    event topic (already prefix-free local names), every leaf carrying a
    ``MessageInstance`` with ``isProperty`` (stateful marker) and the Source/Data
    SimpleItem field names. The path segments are joined to form the canonical
    ``topic_id`` -- the same key space ONVIF canonicalises onto -- so a trigger
    stored against a VAPIX descriptor matches an ONVIF (or VAPIX WS) live event.

    Returns an empty list on a malformed response rather than raising.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    topic_set = _find_local(root, "TopicSet")
    if topic_set is None:
        return []
    descriptors: list[EventDescriptor] = []
    seen: set[str] = set()
    _walk_instances(topic_set, [], descriptors, seen)
    return descriptors


def _walk_instances(
    element: ET.Element,
    path: list[str],
    out: list[EventDescriptor],
    seen: set[str],
) -> None:
    """Recursively collect event descriptors from a VAPIX TopicSet subtree."""
    for child in element:
        local = _local(child.tag)
        if not local or local == "MessageInstance":
            continue
        child_path = [*path, local]
        message = _find_direct_child(child, "MessageInstance")
        if message is not None:
            topic_id = eventutil.canonicalize_topic("/".join(child_path))
            if topic_id and topic_id not in seen:
                seen.add(topic_id)
                out.append(_vapix_descriptor(topic_id, message))
        _walk_instances(child, child_path, out, seen)


def _vapix_descriptor(topic_id: str, message: ET.Element) -> EventDescriptor:
    """Build a descriptor from a canonical topic and its MessageInstance node."""
    stateful = _attr_truthy(message.get("isProperty"))
    data_fields = [
        {"name": name, "type": eventutil.infer_field_type(name)}
        for name in _instance_field_names(message)
    ]
    category = eventutil.category_for_topic(topic_id)
    return EventDescriptor(
        topic_id=topic_id,
        raw_topic=topic_id,
        label=eventutil.label_for_topic(topic_id),
        category=category,
        stateful=stateful,
        data_fields=data_fields,
        protocol="vapix",
        requires_app=category == "analytics",
    )


def _instance_field_names(message: ET.Element) -> list[str]:
    """Return the Source + Data SimpleItem ``Name`` values of a MessageInstance.

    Order is preserved (source fields first, then data) and duplicates dropped.
    """
    names: list[str] = []
    for container_local in ("SourceInstance", "DataInstance"):
        container = _find_direct_child(message, container_local)
        if container is None:
            continue
        for item in container:
            if _local(item.tag) != "SimpleItemInstance":
                continue
            name = item.get("Name")
            if name and name not in names:
                names.append(name)
    return names


def parse_ws_event(text: str) -> CameraEvent | None:
    """Parse a VAPIX ``events:notify`` WebSocket text frame into a CameraEvent.

    The frame is JSON of the form::

        {"apiVersion":"1.0","method":"events:notify",
         "params":{"notification":{
           "topic":"tns1:Device/tnsaxis:IO/VirtualInput",
           "timestamp":1782260923190,
           "message":{"source":{"port":"1"},"key":{},"data":{"active":"1"}}}}}

    Returns None for any frame that is not an ``events:notify`` (acks, pings) or
    that carries no topic, so non-event traffic is silently ignored. The topic is
    canonicalised (same dialect handling as ONVIF), the millisecond epoch
    ``timestamp`` is converted to aware-UTC, and the rising-edge ``active`` state
    is resolved from the merged source+data fields.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("method") != "events:notify":
        return None
    params = payload.get("params")
    notification = params.get("notification") if isinstance(params, dict) else None
    if not isinstance(notification, dict):
        return None
    raw_topic = notification.get("topic")
    if not isinstance(raw_topic, str) or not raw_topic:
        return None
    topic_id = eventutil.canonicalize_topic(raw_topic)
    if not topic_id:
        return None
    message = notification.get("message")
    message = message if isinstance(message, dict) else {}
    source = _coerce_str_map(message.get("source"))
    data = _coerce_str_map(message.get("data"))
    category = eventutil.category_for_topic(topic_id)
    # The WS stream does not flag statefulness per frame; treat the presence of a
    # recognised boolean-state field as the stateful signal so the rising edge is
    # resolved when one is present (a stateless pulse carries none, yielding None).
    attrs = {**source, **data}
    active = eventutil.normalize_active(attrs, stateful=True)
    occurred_at = _ws_timestamp(notification.get("timestamp"))
    return CameraEvent(
        topic_id=topic_id,
        category=category,
        source=source,
        data=data,
        active=active,
        occurred_at=occurred_at,
        raw={"topic": raw_topic},
    )


def ws_subscribe_message(topic_filter: str = "//.") -> str:
    """Build the ``events:configure`` subscribe control frame (default: all)."""
    return json.dumps(
        {
            "apiVersion": "1.0",
            "method": "events:configure",
            "params": {"eventFilterList": [{"topicFilter": topic_filter}]},
        }
    )


def _coerce_str_map(value: object) -> dict[str, str]:
    """Coerce a JSON object to ``dict[str, str]``; empty for anything else."""
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _ws_timestamp(value: object) -> datetime:
    """Convert a millisecond-epoch WS timestamp to aware-UTC, else now."""
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value / 1000.0, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return datetime.now(UTC)
    return datetime.now(UTC)


def _attr_truthy(value: str | None) -> bool:
    """Return whether a VAPIX boolean attribute string is truthy."""
    return value is not None and value.strip().lower() in ("true", "1", "yes")


def _local(tag: object) -> str:
    """Return the local part of a possibly-namespaced ElementTree tag."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _find_local(root: ET.Element, local_name: str) -> ET.Element | None:
    """Return the first descendant (or self) whose tag local name matches."""
    if _local(root.tag) == local_name:
        return root
    for element in root.iter():
        if _local(element.tag) == local_name:
            return element
    return None


def _find_direct_child(element: ET.Element, local_name: str) -> ET.Element | None:
    """Return the first *direct* child whose tag local name matches, else None."""
    for child in element:
        if _local(child.tag) == local_name:
            return child
    return None


# -- Hand-rolled RFC6455 WebSocket (events fast-path) -----------------------
#
# A minimal client for the one thing the event stream needs: open the upgrade
# (with a digest retry), send a masked text frame, read server text frames, reply
# to pings, and send a close frame. Built on stdlib sockets via asyncio streams so
# no WebSocket dependency is added. Server frames are unmasked; client frames must
# be masked per the RFC.

_WS_OP_TEXT = 0x1
_WS_OP_CLOSE = 0x8
_WS_OP_PING = 0x9
_WS_OP_PONG = 0xA


class _WebSocketConnection:
    """A minimal WebSocket client connection over an asyncio stream pair."""

    def __init__(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False

    async def send_text(self, text: str) -> None:
        """Send a masked text frame."""
        await self._send_frame(_WS_OP_TEXT, text.encode("utf-8"))

    async def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray()
        header.append(0x80 | opcode)  # FIN + opcode
        length = len(payload)
        mask_bit = 0x80  # client frames are always masked
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(mask_bit | 127)
            header += length.to_bytes(8, "big")
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._writer.write(bytes(header) + masked)
        await self._writer.drain()

    async def receive(self) -> str | None:
        """Read frames until a text frame arrives; return its text, or None.

        Control frames are handled inline: a ping is answered with a pong, a close
        ends the stream (returns None). A connection error also returns None so
        the caller's loop ends cleanly and the listener re-subscribes.
        """
        while True:
            try:
                frame = await self._read_frame()
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return None
            if frame is None:
                return None
            opcode, payload = frame
            if opcode == _WS_OP_TEXT:
                return payload.decode("utf-8", errors="replace")
            if opcode == _WS_OP_PING:
                await self._send_frame(_WS_OP_PONG, payload)
                continue
            if opcode == _WS_OP_CLOSE:
                return None
            # Pong or any other control/continuation frame: ignore and read on.

    async def _read_frame(self) -> tuple[int, bytes] | None:
        first = await self._reader.readexactly(2)
        opcode = first[0] & 0x0F
        masked = bool(first[1] & 0x80)
        length = first[1] & 0x7F
        if length == 126:
            length = int.from_bytes(await self._reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await self._reader.readexactly(8), "big")
        mask = await self._reader.readexactly(4) if masked else b""
        payload = await self._reader.readexactly(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    async def close(self) -> None:
        """Best-effort close frame, then close the transport. Idempotent."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(ConnectionError, OSError):
            await self._send_frame(_WS_OP_CLOSE, b"")
        with contextlib.suppress(OSError):
            self._writer.close()


async def _ws_open_stream(
    host: str,
    port: int,
    ssl_ctx: object | None,
    *,
    url: str,
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open one raw asyncio stream to the camera, mapping failures to CaptureError.

    Factored out so the handshake can use a *fresh* connection for the
    authenticated retry: the digest nonce is not connection-bound, so reopening
    avoids the fragility of having to drain the unauthenticated 401 response body
    off the same socket before the retry can read a clean status line.
    """
    from .base import TimeoutCaptureError

    try:
        return await asyncio.wait_for(
            asyncio.open_connection(host, port, ssl=ssl_ctx), timeout=timeout
        )
    except TimeoutError as exc:
        raise TimeoutCaptureError(f"ws connect timed out: {url}") from exc
    except (ConnectionError, OSError) as exc:
        raise UnreachableCaptureError(f"cannot reach ws endpoint {url}: {exc}") from exc


async def _ws_connect(
    client: httpx.AsyncClient,
    url: str,
    credentials: tuple[str, str] | None,
    timeout: float,
) -> _WebSocketConnection:
    """Open a WebSocket connection, retrying with HTTP digest on a 401.

    Performs the RFC6455 upgrade by hand over a raw asyncio stream: send the GET
    with the upgrade headers, read the status line + headers. On ``101`` the
    connection is established; on ``401`` with credentials the challenge is
    captured, the unauthenticated connection is **closed**, and a **fresh**
    connection is opened for the authenticated GET (the digest nonce is not
    connection-bound, so a new socket is correct and avoids leftover-401-body
    fragility on the original reader). On ``404`` the endpoint is absent and
    :class:`EventNotSupportedError` is raised so the caller can fall back to ONVIF.

    :raises EventNotSupportedError: on a 404 upgrade (older firmware).
    :raises CaptureError: on transport/timeout/handshake failure.
    """
    from .base import OtherCaptureError

    split = urlsplit(url)
    host = split.hostname or ""
    port = split.port or (443 if split.scheme == "https" else 80)
    ssl_ctx = None
    if split.scheme == "https":
        import ssl

        ssl_ctx = ssl.create_default_context()

    reader, writer = await _ws_open_stream(
        host, port, ssl_ctx, url=url, timeout=timeout
    )
    try:
        status, auth_header = await _ws_handshake(
            reader, writer, split, host, port, auth=None
        )
        if status == 401 and credentials is not None:
            auth = _digest_authorization(
                auth_header, credentials, method="GET", path=_ws_request_path(split)
            )
            if auth is not None:
                # The 401 response may carry a body the first reader has not
                # drained; rather than parse and consume it, drop this socket and
                # perform the authenticated upgrade on a clean connection so the
                # retry's status line is read from a fresh stream.
                with contextlib.suppress(OSError):
                    writer.close()
                reader, writer = await _ws_open_stream(
                    host, port, ssl_ctx, url=url, timeout=timeout
                )
                status, _ = await _ws_handshake(
                    reader, writer, split, host, port, auth=auth
                )
        if status == 404:
            raise EventNotSupportedError(
                "camera does not expose the ws-data-stream events endpoint"
            )
        if status != 101:
            raise OtherCaptureError(f"ws upgrade returned {status} for {url}")
    except BaseException:
        with contextlib.suppress(OSError):
            writer.close()
        raise
    return _WebSocketConnection(reader, writer)


def _ws_request_path(split: object) -> str:
    """Return the request-target (path?query) for a parsed URL."""
    path = getattr(split, "path", "") or "/"
    query = getattr(split, "query", "")
    return f"{path}?{query}" if query else path


async def _ws_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    split: object,
    host: str,
    port: int,
    *,
    auth: str | None,
) -> tuple[int, str]:
    """Send one upgrade request and read the status + WWW-Authenticate header.

    Returns ``(status_code, www_authenticate)``. The Sec-WebSocket-Key is fresh
    per request; the accept value is not strictly validated against it here (the
    camera is trusted post-auth and the status code is the operative signal).
    """
    client_key = base64.b64encode(os.urandom(16)).decode("ascii")
    request_path = _ws_request_path(split)
    host_header = f"{host}:{port}"
    lines = [
        f"GET {request_path} HTTP/1.1",
        f"Host: {host_header}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {client_key}",
        "Sec-WebSocket-Version: 13",
    ]
    if auth is not None:
        lines.append(f"Authorization: {auth}")
    request = "\r\n".join(lines) + "\r\n\r\n"
    writer.write(request.encode("ascii"))
    await writer.drain()

    status_line = await reader.readline()
    status = _parse_status_code(status_line)
    www_authenticate = ""
    while True:
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        decoded = line.decode("latin-1").strip()
        if decoded.lower().startswith("www-authenticate:"):
            www_authenticate = decoded.split(":", 1)[1].strip()
    return status, www_authenticate


def _parse_status_code(status_line: bytes) -> int:
    """Extract the numeric status from an HTTP status line, or 0 if unparseable."""
    try:
        return int(status_line.decode("latin-1").split(" ", 2)[1])
    except (IndexError, ValueError):
        return 0


def _digest_authorization(
    challenge: str,
    credentials: tuple[str, str],
    *,
    method: str,
    path: str,
) -> str | None:
    """Compute an HTTP Digest ``Authorization`` header for a WS upgrade.

    Supports the common ``MD5`` / ``auth`` qop the Axis realm uses. Returns None
    when the challenge is not Digest (the caller then has no usable auth and the
    handshake fails on the next status check).
    """
    if not challenge.lower().startswith("digest"):
        return None
    params = _parse_challenge_params(challenge[len("digest") :])
    realm = params.get("realm", "")
    nonce = params.get("nonce", "")
    qop = params.get("qop")
    username, password = credentials

    def _md5(value: str) -> str:
        return hashlib.md5(value.encode("utf-8")).hexdigest()  # noqa: S324

    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{path}")
    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{path}"',
    ]
    if qop:
        cnonce = base64.b64encode(os.urandom(8)).decode("ascii")
        nc = "00000001"
        response = _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}")
        parts += [
            "qop=auth",
            f"nc={nc}",
            f'cnonce="{cnonce}"',
            f'response="{response}"',
        ]
    else:
        response = _md5(f"{ha1}:{nonce}:{ha2}")
        parts.append(f'response="{response}"')
    opaque = params.get("opaque")
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


def _parse_challenge_params(text: str) -> dict[str, str]:
    """Parse the comma-separated ``k="v"`` params of a Digest challenge."""
    params: dict[str, str] = {}
    for match in _CHALLENGE_PARAM_RE.finditer(text):
        params[match.group(1).lower()] = match.group(2) or match.group(3) or ""
    return params


_CHALLENGE_PARAM_RE = re.compile(r'(\w+)=(?:"([^"]*)"|([^,\s]+))')


class VapixAdapter(CameraAdapter):
    """Capture stills from an Axis camera via the VAPIX snapshot CGI."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        address: str,
        credentials: tuple[str, str] | None = None,
        snapshot_uri: str | None = None,
        default_resolution: str | None = None,
        compression: int | None = None,
        timeout: float = 10.0,
        stream_id: str | None = None,
    ) -> None:
        self._client = client
        self._address = address
        self._credentials = credentials
        self._snapshot_uri = snapshot_uri
        self._resolution = default_resolution
        self._compression = compression
        self._timeout = timeout
        # The selected stream profile's id (its Axis ``Name``), or None to use
        # the camera default. When set, capture() resolves it to the resolution
        # and compression the profile implies and applies them to the snapshot.
        self._stream_id = stream_id

    async def _stream_snapshot_knobs(self) -> tuple[str | None, int | None]:
        """Resolve the selected stream profile to ``(resolution, compression)``.

        Reads the StreamProfile group, finds the profile whose name matches the
        configured ``stream_id``, and extracts the snapshot-relevant knobs from
        its parameters. A missing profile (renamed/removed on the camera) or an
        unreadable group degrades gracefully to the adapter defaults rather than
        failing the capture -- the project keeps capturing from the default
        stream until the selection is corrected.
        """
        params = await self._read_params(STREAM_PROFILE_GROUP)
        parameters = stream_profile_parameters(params, self._stream_id or "")
        if parameters is None:
            logger.debug(
                "vapix stream profile %r not found; using camera default",
                self._stream_id,
            )
            return self._resolution, self._compression
        resolution, compression = snapshot_knobs_from_parameters(parameters)
        # Fall back to the adapter defaults for any knob the profile omits.
        return (
            resolution if resolution is not None else self._resolution,
            compression if compression is not None else self._compression,
        )

    async def capture(self) -> CapturedFrame:
        resolution, compression = self._resolution, self._compression
        # Only touch the parameter CGI when a non-default stream is selected, so
        # a default-stream capture is byte-for-byte the single snapshot GET it
        # has always been (no extra request, identical URL).
        if self._stream_id is not None:
            resolution, compression = await self._stream_snapshot_knobs()
        url = build_snapshot_url(
            self._address,
            resolution=resolution,
            compression=compression,
            explicit_snapshot_uri=self._snapshot_uri,
        )
        image_bytes = await http_get_image(
            self._client, url, self._credentials, self._timeout
        )
        frame = frame_from_bytes(image_bytes)
        # The frame is already in hand; scene metadata is a pure bonus. Collect
        # it best-effort and attach it, but never let it fail or stall the
        # capture -- on any problem the frame is returned with no metadata.
        frame.scene_metadata = await self._collect_scene_metadata(frame)
        return frame

    async def _collect_scene_metadata(
        self, frame: CapturedFrame
    ) -> dict[str, object] | None:
        """Best-effort scene-metadata snapshot for a just-captured frame.

        Performs a single short-timeout read of the camera's scene/image
        parameter group and returns a versioned envelope describing the effective
        scene settings, plus the frame's own captured resolution (free -- no
        extra request). The read is intentionally cheap and bounded:

        * exactly one parameter query, with a short dedicated timeout so it can
          never consume the surrounding per-capture budget;
        * any failure -- a timeout, an unreachable camera, an auth rejection, or
          anything unexpected -- degrades to ``None`` (no metadata) and the
          capture still succeeds.

        ``_read_params`` absorbs every reachability/parse failure into an empty
        result, so an empty read is indistinguishable from a failed one: both are
        treated as "no metadata" (``None``) here, satisfying the rule that any
        failure leaves the frame without scene metadata. An envelope is returned
        only when the camera actually reported scene parameters.
        """
        try:
            params = await self._read_params(
                SCENE_PARAM_GROUP, timeout=SCENE_METADATA_TIMEOUT
            )
            if not params:
                # A failed or empty read -- no metadata for this frame.
                return None
            envelope: dict[str, object] = {
                "schema_version": SCENE_METADATA_SCHEMA_VERSION,
                "source": "vapix",
                "captured_resolution": f"{frame.width}x{frame.height}",
            }
            envelope.update(scene_fields_from_params(params))
            return envelope
        except Exception as exc:  # noqa: BLE001 - metadata must never fail capture
            logger.debug("vapix scene-metadata collection failed: %s", exc)
            return None

    async def list_stream_profiles(self) -> StreamProfileResult:
        # Read the StreamProfile group; a reachability/parse failure surfaces as
        # an empty group from _read_params (it catches CaptureError and returns
        # {}), which we report as a clean ok=False so a caller never crashes.
        try:
            params = await self._read_params(STREAM_PROFILE_GROUP)
        except CaptureError as exc:
            return StreamProfileResult(profiles=[], ok=False, message=exc.message)
        if not params:
            return StreamProfileResult(
                profiles=[],
                ok=False,
                message="could not read stream profiles from the camera",
            )
        return StreamProfileResult(profiles=parse_stream_profiles(params), ok=True)

    async def validate_connection(self) -> ValidationResult:
        try:
            await self.capture()
        except CaptureError as exc:
            return ValidationResult(ok=False, reason=exc.reason, message=exc.message)
        return ValidationResult(
            ok=True, reason=None, message="snapshot retrieved successfully"
        )

    async def list_ptz_presets(self) -> PTZPresetsResult:
        # Enumerate the saved preset positions, then decide PTZ support: a camera
        # is positionable if it exposes presets OR its PTZ group reports the
        # mechanism is enabled (a PTZ camera with no presets defined yet still
        # supports raw moves). Every read can fail on an unreachable or non-Axis
        # camera; http_get_image raises CaptureError, so the whole enumeration is
        # wrapped and degrades to a clean ok=False rather than ever raising.
        query = urlencode({"query": "presetposall"})
        url = f"{_base_url(self._address)}{PTZ_PATH}?{query}"
        try:
            body = await http_get_image(
                self._client, url, self._credentials, self._timeout
            )
        except CaptureError as exc:
            return PTZPresetsResult(
                presets=[], ptz_supported=False, ok=False, message=exc.message
            )
        presets = parse_ptz_presets(body.decode("utf-8", errors="replace"))
        # _read_params absorbs its own failures into {}, so this never raises; a
        # failed PTZ-group read simply contributes no "enabled" signal, and the
        # presence of presets still establishes support.
        ptz_params = await self._read_params(PTZ_PARAM_GROUP)
        ptz_supported = bool(presets) or ptz_enabled_from_params(ptz_params)
        return PTZPresetsResult(presets=presets, ptz_supported=ptz_supported, ok=True)

    async def move_to(
        self,
        *,
        preset_id: str | None = None,
        pan: float | None = None,
        tilt: float | None = None,
        zoom: float | None = None,
    ) -> None:
        # Nothing to position to: a no-op, matching the base contract. A paramless
        # ptz.cgi request would draw an Error body and fail closed for no reason.
        if preset_id is None and pan is None and tilt is None and zoom is None:
            return
        if preset_id is not None:
            url = build_ptz_goto_url(self._address, preset_id)
        else:
            url = build_ptz_move_url(self._address, pan=pan, tilt=tilt, zoom=zoom)
        # Fail closed on every failure mode. http_get_image raises CaptureError
        # on a transport/auth/non-2xx failure; Axis also reports a rejected move
        # with a 2xx status and an Error body, so the body is inspected too. Only
        # a clean success reaches the settle wait -- a failure never sleeps.
        try:
            body = await http_get_image(
                self._client, url, self._credentials, self._timeout
            )
        except CaptureError as exc:
            raise PTZError(f"PTZ move failed: {exc.message}") from exc
        text = body.decode("utf-8", errors="replace")
        if is_ptz_error_body(text):
            raise PTZError(f"camera rejected PTZ move: {text.strip()}")
        # Give the physical dome time to arrive before the caller captures.
        await asyncio.sleep(PTZ_SETTLE_SECONDS)

    async def _read_params(
        self, group: str, *, timeout: float | None = None
    ) -> dict[str, str]:
        """Fetch and parse a parameter group, returning {} on any failure.

        ``timeout`` overrides the adapter's default request timeout for this one
        read. The scene-metadata read on the capture hot path passes a short
        timeout so a slow camera can never eat the surrounding capture budget;
        all other callers fall back to ``self._timeout``.
        """
        query = urlencode({"action": "list", "group": group})
        url = f"{_base_url(self._address)}{PARAM_PATH}?{query}"
        try:
            text = (
                await http_get_image(
                    self._client,
                    url,
                    self._credentials,
                    timeout if timeout is not None else self._timeout,
                )
            ).decode("utf-8", errors="replace")
        except CaptureError as exc:
            logger.debug("vapix param query failed for %s: %s", group, exc.message)
            return {}
        return parse_param_response(text)

    async def get_geolocation(self) -> GeoLocation | None:
        # Current Axis firmware serves the location from the dedicated
        # Geolocation API; query it first.
        location = await self._read_geolocation_api()
        if location is not None:
            return location
        # Fall back to the legacy parameter group for older devices that predate
        # the API (the API answers with an error there).
        params = await self._read_params("Geolocation")
        return geolocation_from_params(params)

    async def _read_geolocation_api(self) -> GeoLocation | None:
        """Query the Axis Geolocation API and parse its position document.

        Best-effort: an unreachable camera or a non-success response (e.g. the
        device lacks the API) degrades to None so the caller can fall back to the
        legacy parameter group rather than failing.
        """
        url = f"{_base_url(self._address)}{GEOLOCATION_PATH}"
        try:
            body = await http_get_image(
                self._client, url, self._credentials, self._timeout
            )
        except CaptureError as exc:
            logger.debug("vapix geolocation query failed: %s", exc.message)
            return None
        return geolocation_from_position_xml(body.decode("utf-8", errors="replace"))

    async def get_device_hostname(self) -> str | None:
        # Read the Network parameter group and pull out the configured hostname.
        # _read_params absorbs every reachability/auth/parse failure into an empty
        # dict, so an unreachable or non-Axis camera degrades to None here rather
        # than raising -- matching the best-effort contract of the other metadata
        # reads on this adapter.
        params = await self._read_params(NETWORK_PARAM_GROUP)
        return hostname_from_params(params)

    async def capabilities(self) -> CameraCapabilities:
        params = await self._read_params("Properties.Image")
        resolutions: list[str] = []
        raw = _find_param(params, "Resolution")
        if raw:
            resolutions = [r.strip() for r in raw.split(",") if r.strip()]
        return CameraCapabilities(
            supported_resolutions=resolutions,
            compression_range=COMPRESSION_RANGE,
        )

    # -- Events -------------------------------------------------------------

    def _guard_event_url(self, url: str) -> None:
        """Validate an event URL against the camera deny-list, fail-closed.

        Camera/scan policy (admin private opt-in honoured; loopback/link-local/
        metadata never relaxed). A denied target surfaces as
        :class:`UnreachableCaptureError`.
        """
        from ..runtime import get_context

        ssrf = get_context().settings.ssrf
        try:
            assert_allowed_url(
                url,
                allow_private=True,
                allowed_private_subnets=ssrf.allowed_private_subnets,
            )
        except SsrfError as exc:
            raise UnreachableCaptureError(f"event URL blocked: {exc}") from exc

    async def list_event_topics(self) -> list[EventDescriptor]:
        # Query GetEventInstances over the guarded HTTP path (plain digest, the
        # same realm snapshot uses). Every failure mode degrades to an empty list
        # so a caller can enumerate inline without a try/except.
        url = f"{_base_url(self._address)}{VAPIX_SERVICES_PATH}"
        try:
            self._guard_event_url(url)
            text = await self._post_soap(url, get_event_instances_body())
        except CaptureError as exc:
            logger.debug("vapix event-instance enumeration failed: %s", exc.message)
            return []
        return parse_event_instances(text)

    async def _post_soap(self, url: str, body: str) -> str:
        """POST a SOAP body, handling a digest challenge, returning the text.

        Mirrors :func:`http_get_image`'s auth dance for a POST: an unauthenticated
        request first, then a retry with the advertised scheme on a 401. Maps
        transport/timeout/auth/status failures to :class:`CaptureError` so the
        caller's recoverable-failure handling applies.
        """
        from .http_jpeg import _auth_from_challenge, classify_http_status

        headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
        try:
            response = await self._client.post(
                url, content=body, headers=headers, timeout=self._timeout
            )
            if response.status_code == 401 and self._credentials is not None:
                challenge = response.headers.get("www-authenticate", "")
                auth = _auth_from_challenge(challenge, *self._credentials)
                response = await self._client.post(
                    url,
                    content=body,
                    headers=headers,
                    timeout=self._timeout,
                    auth=auth,
                )
        except httpx.TimeoutException as exc:
            from .base import TimeoutCaptureError

            raise TimeoutCaptureError(f"vapix event call timed out: {url}") from exc
        except httpx.TransportError as exc:
            raise UnreachableCaptureError(
                f"cannot reach vapix device {url}: {exc}"
            ) from exc
        from .base import AuthCaptureError, OtherCaptureError, ValidationFailure

        failure = classify_http_status(response.status_code)
        if failure is ValidationFailure.AUTH:
            raise AuthCaptureError(
                f"authentication rejected ({response.status_code}) for {url}"
            )
        if failure is not None:
            raise OtherCaptureError(
                f"unexpected status {response.status_code} from {url}"
            )
        return response.text

    def open_event_source(self) -> AsyncIterator[CameraEvent]:
        # WebSocket fast-path. The async generator defers the upgrade and the
        # subscribe to its first iteration (I/O-free construction) and raises
        # EventNotSupportedError when the endpoint 404s (older firmware) so a
        # caller can fall back to the ONVIF PullPoint source.
        return self._ws_events()

    async def _ws_events(self) -> AsyncIterator[CameraEvent]:
        """Yield canonical events from the VAPIX WebSocket stream until cancelled.

        Performs the RFC6455 handshake (with a digest retry on the 401
        challenge), sends the ``events:configure`` subscribe frame, then reads
        masked-from-server text frames, parsing each ``events:notify`` into a
        :class:`CameraEvent`. Server ping frames are answered with a pong; on
        teardown a close frame is sent. A ``404`` upgrade (older firmware lacking
        the endpoint) raises :class:`EventNotSupportedError`.

        :raises EventNotSupportedError: when the WS endpoint is absent (404).
        :raises CaptureError: on a guard denial, transport, or handshake failure.
        """
        url = f"{_base_url(self._address)}{WS_DATA_STREAM_PATH}"
        self._guard_event_url(url)
        connection = await _ws_connect(
            self._client, url, self._credentials, self._timeout
        )
        try:
            await connection.send_text(ws_subscribe_message())
            while True:
                frame = await connection.receive()
                if frame is None:
                    return
                event = parse_ws_event(frame)
                if event is not None:
                    yield event
        finally:
            await connection.close()

    async def close(self) -> None:
        # The HTTP client is owned by the caller; nothing to release here.
        return None
