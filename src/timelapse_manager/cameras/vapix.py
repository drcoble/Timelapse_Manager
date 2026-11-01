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
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import SplitResult, parse_qsl, urlencode, urlsplit
from xml.etree import ElementTree as ET

import httpx

from ..security.credential_binding import split_userinfo
from ..security.pinned_http import CONTAINED_MESSAGE, address_inside_subnets
from ..security.ssrf import (
    SsrfError,
    pinned_addresses_async,
    resolve_and_check_pinned_async,
)
from . import events as eventutil
from ._bounded import (
    SOAP_RESPONSE_MAX_BYTES,
    TEXT_RESPONSE_MAX_BYTES,
    read_capped_response,
    safe_fromstring,
    walk_named_descendants,
)
from .base import (
    PTZ_SETTLE_MAX_SECONDS,
    AdapterCache,
    AuthCaptureError,
    CameraAdapter,
    CameraCapabilities,
    CameraEvent,
    CapturedFrame,
    CaptureError,
    EventDescriptor,
    EventNotSupportedError,
    GeoLocation,
    GeolocationAccessDenied,
    OtherCaptureError,
    PTZError,
    PtzPosition,
    PTZPreset,
    PTZPresetsResult,
    StreamProfile,
    StreamProfileResult,
    UnreachableCaptureError,
    ValidationResult,
    valid_coordinates,
)
from .clients import pool_exhausted_error, request_timeout
from .host_resolution import camera_allowed_subnets, check_camera_url_async
from .http_auth import CameraAuthState, UnsupportedAuthSchemeError
from .http_jpeg import frame_from_bytes, http_get_image
from .tls import (
    PIN_MISMATCH_MESSAGE,
    camera_ssl_context,
    is_pinned_context,
    pinned_context_of,
    resolve_tls_ca_bundle,
)

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
# Axis documents the Geolocation API as "Security level: Administrator" for both
# get and set: a viewer or operator account is refused (the fleet answers 401).
# Surfaced verbatim in GeolocationAccessDenied so the UI can say which role can
# read the position. Capture itself never needs it.
GEOLOCATION_REQUIRED_ROLE = "Administrator"

# The HTTP status inside an AuthCaptureError message, e.g. "authentication
# rejected (401) for ...". The shared HTTP helper carries the status only in its
# message, and it is deliberately left unchanged (it is on the capture path), so
# the status is recovered here for the denial's diagnostics.
_AUTH_STATUS_PATTERN = re.compile(r"\((\d{3})\)")

# Axis PTZ commands address a specific camera channel; channel 1 is the sole
# channel on a single-head PTZ dome and the correct default for the rest.
PTZ_CAMERA = "1"

# How long to wait after a positioning command so the physical dome has settled
# on the new position before the caller captures from it. A bounded pause that
# trades a few seconds of latency for a frame taken at the intended position
# rather than mid-travel.
PTZ_SETTLE_SECONDS = 3.0
# Settling by observation (#69 CAM-R4): after a move the camera's position is
# polled every PTZ_SETTLE_POLL_SECONDS until two consecutive readings agree, no
# sooner than PTZ_SETTLE_MIN_SECONDS (so a dome that has not started moving yet
# is not mistaken for one that has arrived) and no later than
# PTZ_SETTLE_MAX_SECONDS. A camera that does not report its position falls back
# to the fixed PTZ_SETTLE_SECONDS pause.
PTZ_SETTLE_POLL_SECONDS = 0.5
PTZ_SETTLE_MIN_SECONDS = 1.0

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

# Per-request timeout for the best-effort scene-metadata read. The read runs
# after the frame is in hand, outside the capture budget, and the capture path
# also bounds it with a total timeout (``SCENE_METADATA_TOTAL_SECONDS`` in the
# supervisor), so a slow parameter CGI degrades to "no metadata" and never costs
# the frame.
SCENE_METADATA_TIMEOUT = 2.0

# Total time the StreamProfile read may take before a capture with a selected
# stream falls back to the camera default. This read sits inside the capture
# budget (it decides the snapshot URL), so it is bounded well under it.
STREAM_PROFILE_TIMEOUT = 2.0

# ``CapturedFrame.capture_notes`` values for a stream profile that could not be
# applied; the capture continues at the camera default (owner decision OD-2).
STREAM_PROFILE_MISSING = "stream_profile_missing"
STREAM_PROFILE_UNREADABLE = "stream_profile_unreadable"
STREAM_PROFILE_TIMED_OUT = "stream_profile_timeout"

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


def _split_address(address: str) -> SplitResult:
    """Parse a camera address the one way every VAPIX URL is built from.

    A scheme-less address is parsed as ``http://<address>``. Both
    :func:`_base_url` (which the adapter connects to) and
    :func:`admin_read_host` (which keys the admin-read limiter) read the host
    from this one parse, so they can never disagree about the target.
    """
    if address.startswith(("http://", "https://")):
        return urlsplit(address)
    return urlsplit(f"http://{address}")


def _base_url(address: str) -> str:
    """Return the http base URL for an address, defaulting the scheme to http.

    Any userinfo (``user:pass@``) the operator may have embedded in the address
    is dropped, with or without a scheme: VAPIX authentication is sent in
    request headers, never in the URL, and the composed URL is surfaced/persisted
    as the snapshot URI (and logged), so it must never carry credentials. The
    host comes from :func:`_split_address`. A scheme-less address keeps its path
    (a trailing ``/`` is stripped), as it always has; a query or fragment is
    dropped.
    """
    split = _split_address(address)
    host = split.hostname or ""
    if ":" in host:  # IPv6 literal -- restore the brackets a netloc needs.
        host = f"[{host}]"
    try:
        netloc = f"{host}:{split.port}" if split.port else host
    except ValueError:
        # A malformed port: keep the authority text minus any userinfo, and let
        # the request fail as it always did rather than raising here.
        netloc = split.netloc.rpartition("@")[2]
    if address.startswith(("http://", "https://")):
        return f"{split.scheme}://{netloc}"
    return f"http://{netloc}{split.path}".rstrip("/")


# Characters an admin-read address may not contain at all: each makes URL
# parsers disagree about where the host ends.
_AMBIGUOUS_ADDRESS_CHARS = ("?", "#", "\\")


def admin_read_host(address: str) -> str:
    """Return the host a VAPIX adapter will connect to, rejecting ambiguity.

    Used by the one-time admin read (#68) before it resolves and rate-limits
    the target. The host is taken from the same parse as :func:`_base_url`, and
    an address whose host is ambiguous is refused outright: one carrying ``?``,
    ``#`` or ``\\`` anywhere, userinfo (``@``) in its authority, no host, or a
    malformed port.

    :raises ValueError: when the address is ambiguous or has no host.
    """
    if any(char in address for char in _AMBIGUOUS_ADDRESS_CHARS):
        raise ValueError("camera address contains an ambiguous character")
    split = _split_address(address)
    if "@" in split.netloc:
        raise ValueError("camera address carries userinfo")
    host = split.hostname
    if not host:
        raise ValueError("camera address has no host")
    _ = split.port  # raises ValueError on a malformed port
    return host


def _auth_status(exc: AuthCaptureError) -> int | None:
    """Return the HTTP status carried in an auth error's message, if any."""
    match = _AUTH_STATUS_PATTERN.search(exc.message)
    return int(match.group(1)) if match else None


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


def _camera_location(latitude: str, longitude: str) -> GeoLocation | None:
    """Parse a device-reported coordinate pair, or None when unset or invalid.

    Axis reports ``0`` (or an empty string) for both coordinates when no
    position is configured, so ``(0, 0)`` is treated as unset rather than as a
    real site in the Gulf of Guinea: no camera here sits at "Null Island", and
    accepting it would silently skew every solar anchor for that camera.

    A value that is not finite (``nan``, ``inf``) or out of range is rejected
    the same way: ``float()`` accepts those spellings, and the device is
    untrusted input.
    """
    try:
        lat = float(latitude)
        lon = float(longitude)
    except ValueError:
        return None
    if lat == 0.0 and lon == 0.0:
        return None
    if not valid_coordinates(lat, lon):
        return None
    return GeoLocation(latitude=lat, longitude=lon, source="camera")


def geolocation_from_params(params: dict[str, str]) -> GeoLocation | None:
    """Extract a :class:`GeoLocation` from Axis parameter values, if present.

    Older Axis devices may expose ``Geolocation.Latitude`` /
    ``Geolocation.Longitude`` (also seen as ``root.Geolocation.*``). Returns None
    when absent, unparseable, or the unset ``(0, 0)`` default.
    """
    latitude = _find_param(params, "Latitude")
    longitude = _find_param(params, "Longitude")
    if latitude is None or longitude is None:
        return None
    return _camera_location(latitude, longitude)


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
    ``ValidPosition`` false, reports the unset ``(0, 0)`` default, or cannot be
    parsed (including a document over the size cap or carrying a DTD; see
    :func:`._bounded.safe_fromstring`). Heading is not modelled and is ignored.
    """
    root = safe_fromstring(text, max_bytes=TEXT_RESPONSE_MAX_BYTES)
    if root is None:
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
    return _camera_location(latitude, longitude)


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


def build_ptz_position_url(address: str) -> str:
    """Build the VAPIX URL that reads the current pan/tilt/zoom position."""
    query = urlencode({"camera": PTZ_CAMERA, "query": "position"})
    return f"{_base_url(address)}{PTZ_PATH}?{query}"


def parse_ptz_position(text: str) -> PtzPosition | None:
    """Parse a ``ptz.cgi?query=position`` body (``pan=..`` lines).

    Returns ``None`` when no axis could be read (an error body, say).
    """
    values: dict[str, float] = {}
    for line in text.splitlines():
        key, sep, raw = line.strip().partition("=")
        if not sep or key not in ("pan", "tilt", "zoom"):
            continue
        try:
            values[key] = float(raw.strip())
        except ValueError:
            continue
    if not values:
        return None
    return PtzPosition(
        pan=values.get("pan"), tilt=values.get("tilt"), zoom=values.get("zoom")
    )


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

    Returns an empty list on a malformed, oversized or DTD-carrying response
    (see :func:`._bounded.safe_fromstring`) rather than raising.
    """
    root = safe_fromstring(xml_text)
    if root is None:
        return []
    topic_set = _find_local(root, "TopicSet")
    if topic_set is None:
        return []
    return _walk_instances(topic_set)


def _walk_instances(topic_set: ET.Element) -> list[EventDescriptor]:
    """Collect event descriptors from a VAPIX TopicSet subtree.

    Every element on the path to a ``MessageInstance`` contributes one path
    segment; ``MessageInstance`` subtrees themselves are not topics. The walk is
    iterative and stops at :data:`._bounded.XML_MAX_DEPTH` levels, so a deeply
    nested response yields the topics above the cap instead of raising
    ``RecursionError`` (#69 CAM-R12).
    """
    descriptors: list[EventDescriptor] = []
    seen: set[str] = set()
    for element, path in walk_named_descendants(
        topic_set, skip=frozenset({"MessageInstance"})
    ):
        message = _find_direct_child(element, "MessageInstance")
        if message is None:
            continue
        topic_id = eventutil.canonicalize_topic("/".join(path))
        if topic_id and topic_id not in seen:
            seen.add(topic_id)
            descriptors.append(_vapix_descriptor(topic_id, message))
    return descriptors


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

# How long each step of closing the event WebSocket (the close frame, then the
# transport) may take before the transport is aborted.
_WS_CLOSE_TIMEOUT = 1.0
_WS_OP_TEXT = 0x1
_WS_OP_CLOSE = 0x8
_WS_OP_PING = 0x9
_WS_OP_PONG = 0xA

# RFC 6455 section 4.2.2: the server proves it read this handshake by answering
# with base64(SHA-1(client key + this GUID)) in ``Sec-WebSocket-Accept``.
_WS_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Most header lines one handshake response may carry. Each line is already
# bounded by the stream reader's 64 KiB limit; this bounds how many there are.
_WS_MAX_HEADER_LINES = 100


def _new_ws_key() -> str:
    """Return a fresh random ``Sec-WebSocket-Key`` (16 bytes, base64)."""
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _ws_expected_accept(client_key: str) -> str:
    """Return the ``Sec-WebSocket-Accept`` value a server must send for a key."""
    digest = hashlib.sha1(
        (client_key + _WS_ACCEPT_GUID).encode("ascii"), usedforsecurity=False
    ).digest()
    return base64.b64encode(digest).decode("ascii")


@dataclass(frozen=True)
class _WsLimits:
    """Resolved per-connection bounds for the untrusted event WebSocket.

    Carries the keepalive/idle and frame-size ceilings from settings into the
    handshake and connection so the socket-level code stays free of any context
    lookup (and stays trivially testable with explicit values).
    """

    ping_interval: float
    read_timeout: float
    max_frame_bytes: int


def _resolve_ws_limits() -> _WsLimits:
    """Build the event-WebSocket bounds from the active capture settings."""
    from ..runtime import get_context

    capture = get_context().settings.capture
    return _WsLimits(
        ping_interval=capture.ws_ping_interval_seconds,
        read_timeout=capture.ws_read_timeout_seconds,
        max_frame_bytes=capture.max_ws_frame_bytes,
    )


class _WebSocketConnection:
    """A minimal WebSocket client connection over an asyncio stream pair.

    The camera socket is untrusted, so two safeguards bound it:

    * **Idle/read timeout.** ``receive`` reads each frame under a ``ping_interval``
      deadline. On the first silent window it sends a keepalive ping and waits one
      more window; a second consecutive silent window (no frame, no pong) is taken
      as a dead socket and ends the stream (returns ``None``) so the caller's loop
      reconnects. Any received frame -- including a pong -- resets the window, so a
      legitimately quiet but live camera is never dropped.
    * **Frame-size cap.** A frame whose declared length exceeds ``max_frame_bytes``
      is rejected before its payload is read, so an untrusted 64-bit length cannot
      drive an unbounded allocation.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        ping_interval: float,
        read_timeout: float,
        max_frame_bytes: int,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._closed = False
        self._ping_interval = ping_interval
        self._read_timeout = read_timeout
        self._max_frame_bytes = max_frame_bytes

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

        After ``ping_interval`` of silence a keepalive ping is sent; the socket is
        declared dead only once the total silence reaches ``read_timeout`` with no
        frame and no pong in between. The post-ping window is the remainder of the
        budget, so the two knobs are independent (``ping_interval`` controls when
        the probe fires, ``read_timeout`` the total budget). Any frame -- including
        a pong -- resets the budget, so a quiet-but-live camera is never dropped.
        """
        pinged = False
        while True:
            # Before the ping, wait the ping interval; after it, wait whatever
            # remains of the total read-timeout budget.
            window = (
                self._ping_interval
                if not pinged
                else max(0.0, self._read_timeout - self._ping_interval)
            )
            # asyncio.timeout, not wait_for: on 3.11 wait_for can return a frame
            # that arrives with our cancel and drop the cancel (#33).
            try:
                async with asyncio.timeout(window):
                    frame = await self._read_frame()
            except TimeoutError:
                if not pinged:
                    # First silence: probe with a ping and wait the remainder. The
                    # write is bounded too, so a wedged writer cannot hang here.
                    pinged = True
                    try:
                        async with asyncio.timeout(self._ping_interval):
                            await self._send_frame(_WS_OP_PING, b"")
                    except (ConnectionError, OSError, TimeoutError):
                        return None
                    continue
                # No frame and no pong within the full budget: socket is dead.
                logger.warning(
                    "event websocket idle past %.0fs read timeout; reconnecting",
                    self._read_timeout,
                )
                return None
            except (asyncio.IncompleteReadError, ConnectionError, OSError):
                return None
            # A delivered frame (text, control, or pong) resets the budget.
            pinged = False
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
        # Reject an oversized frame before allocating/reading its payload: the
        # declared length is untrusted (a 64-bit value the camera controls). This
        # raises a CaptureError (not an OSError), so it propagates past receive()
        # to the listener's backoff loop rather than triggering a tight reconnect.
        if length > self._max_frame_bytes:
            from .base import OtherCaptureError

            raise OtherCaptureError(
                f"event frame length {length} exceeds the "
                f"{self._max_frame_bytes}-byte limit"
            )
        mask = await self._reader.readexactly(4) if masked else b""
        payload = await self._reader.readexactly(length) if length else b""
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    async def close(self) -> None:
        """Best-effort close frame, then close the transport. Idempotent.

        Bounded so a wedged camera cannot hold up teardown (a listener stop, a
        shutdown): the close frame gets :data:`_WS_CLOSE_TIMEOUT` and so does the
        transport's close; if either does not finish, the transport is aborted
        (#69 CAM-R14).
        """
        if self._closed:
            return
        self._closed = True
        try:
            async with asyncio.timeout(_WS_CLOSE_TIMEOUT):
                await self._send_frame(_WS_OP_CLOSE, b"")
        except (ConnectionError, OSError, TimeoutError):
            pass
        try:
            self._writer.close()
            async with asyncio.timeout(_WS_CLOSE_TIMEOUT):
                await self._writer.wait_closed()
        except (ConnectionError, OSError, TimeoutError):
            # A wedged transport never finishes a graceful close: drop it.
            with contextlib.suppress(Exception):
                self._writer.transport.abort()


async def _ws_open_stream(
    connect_host: str,
    port: int,
    ssl_ctx: object | None,
    *,
    server_hostname: str | None,
    url: str,
    timeout: float,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open one raw asyncio stream to the camera, mapping failures to CaptureError.

    The connection is opened to ``connect_host`` -- a pre-validated IP literal,
    not the original hostname -- so DNS cannot be rebound to a denied address
    between the deny-list check and this connect. For a TLS endpoint
    ``server_hostname`` carries the *original* hostname so certificate
    verification (and name-based virtual hosting) still resolve against the name
    the operator configured rather than the IP we dialled. It must be ``None`` for
    a plaintext connection (``open_connection`` rejects an SNI without TLS).

    Factored out so the handshake can use a *fresh* connection for the
    authenticated retry: the digest nonce is not connection-bound, so reopening
    avoids the fragility of having to drain the unauthenticated 401 response body
    off the same socket before the retry can read a clean status line.
    """
    from .base import TimeoutCaptureError

    # asyncio.timeout, not wait_for: on 3.11 wait_for can hand back a stream
    # that connects as we are cancelled and drop the cancel (#33).
    try:
        async with asyncio.timeout(timeout):
            return await asyncio.open_connection(
                connect_host, port, ssl=ssl_ctx, server_hostname=server_hostname
            )
    except TimeoutError as exc:
        raise TimeoutCaptureError(f"ws connect timed out: {url}") from exc
    except (ConnectionError, OSError) as exc:
        if isinstance(exc, ssl.SSLCertVerificationError) and is_pinned_context(ssl_ctx):
            # A pinned camera: a fixed sentence, nothing from the handshake.
            raise UnreachableCaptureError(
                f"cannot reach ws endpoint {url}: {PIN_MISMATCH_MESSAGE}"
            ) from None
        raise UnreachableCaptureError(f"cannot reach ws endpoint {url}: {exc}") from exc


async def _ws_default_contained(url: str, connect_host: str) -> bool:
    """Whether the default login may go to ``connect_host`` for ``url`` (#268).

    The same rule as credential binding: **every** address the URL's host has
    (from the shared validated resolution) and the address dialled must be
    inside the live allowed private subnets. A host that does not resolve or is
    denied is refused.
    """
    subnets = camera_allowed_subnets()
    if not address_inside_subnets(connect_host, subnets):
        return False
    host = urlsplit(url).hostname or ""
    try:
        answer = await pinned_addresses_async(
            host, allow_private=True, allowed_private_subnets=subnets
        )
    except SsrfError:
        return False
    return bool(answer) and all(
        address_inside_subnets(address, subnets) for address in answer
    )


async def _ws_connect(
    client: httpx.AsyncClient,
    url: str,
    connect_host: str,
    credentials: tuple[str, str] | None,
    timeout: float,
    limits: _WsLimits,
    auth_state: CameraAuthState | None = None,
    contain_default: bool = False,
) -> _WebSocketConnection:
    """Open a WebSocket connection, retrying with Basic/Digest auth on a 401.

    The socket is opened to ``connect_host`` (a pre-validated IP literal) rather
    than re-resolving the URL host, so the deny-list check cannot be undone by a
    DNS rebind between check and connect; for TLS the original hostname is still
    presented as SNI and in the ``Host`` header so certificate verification holds.

    Performs the RFC6455 upgrade by hand over the raw asyncio stream: send the GET
    with the upgrade headers (and the ``Authorization`` the auth state already
    negotiated with this camera, if any), read the status line + headers. On
    ``101`` the connection is established; on ``401`` with credentials the
    challenge is answered through :class:`~.http_auth.CameraAuthState` -- every
    ``WWW-Authenticate`` line is read, Digest wins over Basic, and Basic over
    plain HTTP follows the camera's policy -- the challenged connection is
    **closed**, and a **fresh** connection is opened for the authenticated GET
    (the nonce is not connection-bound, so a new socket is correct and avoids
    leftover-401-body fragility on the original reader). A second ``401`` is an
    authentication failure. On ``404`` the endpoint is absent, and on a ``401``
    whose only schemes we cannot speak, :class:`EventNotSupportedError` is raised
    so the caller can fall back to ONVIF PullPoint.

    :raises EventNotSupportedError: on a 404 upgrade (older firmware) or a 401
        whose auth scheme is genuinely unsupported (neither Basic nor Digest).
    :raises AuthCaptureError: the camera rejected the answered challenge, or
        offered only Basic over plain HTTP where the policy refuses it.
    :raises CaptureError: on transport/timeout/handshake failure.

    ``contain_default`` is set when ``credentials`` are the bound global default
    login: the socket is then opened only when ``connect_host`` is inside the
    live allowed private subnets (#268), before any request byte is written.
    """
    from .base import AuthCaptureError, OtherCaptureError

    if contain_default and not await _ws_default_contained(url, connect_host):
        raise UnreachableCaptureError(
            f"cannot reach ws endpoint {url}: {CONTAINED_MESSAGE}"
        )
    state = auth_state if auth_state is not None else CameraAuthState()
    split = urlsplit(url)
    host = split.hostname or ""
    port = split.port or (443 if split.scheme == "https" else 80)
    ssl_ctx = None
    server_hostname: str | None = None
    if split.scheme == "https":
        # The same trust source as the camera HTTP clients (#69 CAM-S1): the
        # client's pin when the camera is pinned, else the shared context.
        ssl_ctx = pinned_context_of(client) or camera_ssl_context(
            resolve_tls_ca_bundle()
        )
        # Verify the certificate against the original hostname, not the IP dialled.
        server_hostname = host
    elif pinned_context_of(client) is not None:
        # A pin is only meaningful over TLS: refuse the plain-HTTP downgrade.
        raise UnreachableCaptureError(
            f"cannot reach ws endpoint {url}: "
            "this camera is pinned to a certificate and must use https"
        )

    from .base import TimeoutCaptureError

    reader, writer = await _ws_open_stream(
        connect_host,
        port,
        ssl_ctx,
        server_hostname=server_hostname,
        url=url,
        timeout=timeout,
    )
    try:
        # Bound the handshake reads: a camera that completes the TCP connect but
        # never sends a status line would otherwise hang the readline() forever.
        preemptive = state.preemptive("GET", url, credentials)
        try:
            async with asyncio.timeout(timeout):
                status, challenges = await _ws_handshake(
                    reader, writer, split, host, port, auth=preemptive
                )
        except TimeoutError as exc:
            raise TimeoutCaptureError(f"ws handshake timed out: {url}") from exc
        if status == 401 and credentials is not None:
            try:
                auth = state.respond(challenges, "GET", url, credentials)
            except UnsupportedAuthSchemeError as exc:
                # A 401 we cannot answer (e.g. Negotiate/NTLM): treat the WS path
                # as unavailable so the caller cleanly falls back to PullPoint
                # rather than reconnect-looping on an upgrade that can never 101.
                raise EventNotSupportedError(
                    "camera ws upgrade requires an unsupported auth scheme"
                ) from exc
            # The 401 response may carry a body the first reader has not drained;
            # rather than parse and consume it, drop this socket and perform the
            # authenticated upgrade on a clean connection so the retry's status
            # line is read from a fresh stream.
            with contextlib.suppress(OSError):
                writer.close()
            reader, writer = await _ws_open_stream(
                connect_host,
                port,
                ssl_ctx,
                server_hostname=server_hostname,
                url=url,
                timeout=timeout,
            )
            try:
                async with asyncio.timeout(timeout):
                    status, _ = await _ws_handshake(
                        reader, writer, split, host, port, auth=auth
                    )
            except TimeoutError as exc:
                raise TimeoutCaptureError(f"ws handshake timed out: {url}") from exc
            if status == 401:
                # The camera rejected the answered challenge: the credentials
                # are wrong (CAM-R9), not a generic upgrade failure.
                state.forget(url)
                raise AuthCaptureError(
                    f"authentication rejected (401) for ws upgrade {url}"
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
    return _WebSocketConnection(
        reader,
        writer,
        ping_interval=limits.ping_interval,
        read_timeout=limits.read_timeout,
        max_frame_bytes=limits.max_frame_bytes,
    )


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
) -> tuple[int, list[str]]:
    """Send one upgrade request and read the status + WWW-Authenticate headers.

    Returns ``(status_code, www_authenticate)``, the latter every
    ``WWW-Authenticate`` value in arrival order. The Sec-WebSocket-Key is fresh
    per request. A ``101`` is accepted only when ``Sec-WebSocket-Accept`` matches
    that key (RFC 6455 section 4.1), so a peer that is not a WebSocket server
    answering this request (#69 CAM-S7) is refused.

    The response is untrusted: a status or header line over the stream reader's
    64 KiB limit, or more than :data:`_WS_MAX_HEADER_LINES` header lines, is
    refused as :class:`OtherCaptureError` (#69 CAM-R12) rather than raising an
    unclassified ``ValueError`` into the listener.

    :raises OtherCaptureError: on an oversized response head, or a ``101`` with
        a missing or wrong ``Sec-WebSocket-Accept``.
    """
    client_key = _new_ws_key()
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

    try:
        status, headers = await _read_ws_response_head(reader)
    except (ValueError, asyncio.LimitOverrunError) as exc:
        # StreamReader.readline raises ValueError for a line over its limit.
        raise OtherCaptureError("ws handshake response line too long") from exc
    if status == 101:
        accept = _last(headers.get("sec-websocket-accept"))
        if accept != _ws_expected_accept(client_key):
            raise OtherCaptureError(
                "ws upgrade answered with a missing or wrong Sec-WebSocket-Accept"
            )
    return status, headers.get("www-authenticate", [])


def _last(values: list[str] | None) -> str:
    """Return the last value of a repeated header, or ``""``."""
    return values[-1] if values else ""


async def _read_ws_response_head(
    reader: asyncio.StreamReader,
) -> tuple[int, dict[str, list[str]]]:
    """Read a response status line and headers, up to the blank line.

    Returns the status code (``0`` if unparseable) and every header value keyed
    by lower-cased name, in arrival order. A camera sends one
    ``WWW-Authenticate`` line per scheme it offers, and choosing among them is
    the auth code's job (#69 CAM-S1), so none may be dropped here.

    :raises OtherCaptureError: past :data:`_WS_MAX_HEADER_LINES` header lines.
    """
    status = _parse_status_code(await reader.readline())
    headers: dict[str, list[str]] = {}
    for _ in range(_WS_MAX_HEADER_LINES):
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            return status, headers
        name, _sep, value = line.decode("latin-1").partition(":")
        headers.setdefault(name.strip().lower(), []).append(value.strip())
    raise OtherCaptureError(
        f"ws handshake response has over {_WS_MAX_HEADER_LINES} header lines"
    )


def _parse_status_code(status_line: bytes) -> int:
    """Extract the numeric status from an HTTP status line, or 0 if unparseable."""
    try:
        return int(status_line.decode("latin-1").split(" ", 2)[1])
    except (IndexError, ValueError):
        return 0


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
        knob_cache: AdapterCache | None = None,
        auth_state: CameraAuthState | None = None,
        contain_default: bool = False,
    ) -> None:
        self._client = client
        # True when ``credentials`` are the bound global default: the event
        # WebSocket (a raw socket the HTTP client's containment cannot see) then
        # refuses an address outside the allowed private subnets (#268).
        self._contain_default = contain_default
        # Never keep a ``user:pass@`` from a legacy address: httpx would turn it
        # into a preemptive Basic header on every request (#69 CAM-S2). The
        # startup pass has moved that login into the credential document.
        self._address = split_userinfo(address)[0]
        self._credentials = credentials
        # The camera session's negotiation state (#69 CAM-S1, CAM-P1): every
        # request this adapter makes -- snapshot, CGIs, SOAP, the event
        # WebSocket -- reuses the scheme and Digest nonce negotiated before. A
        # fresh one with the probe policy (never Basic over plain HTTP) when
        # none is given.
        self._auth_state = auth_state if auth_state is not None else CameraAuthState()
        self._snapshot_uri = snapshot_uri
        self._resolution = default_resolution
        self._compression = compression
        self._timeout = timeout
        # The selected stream profile's id (its Axis ``Name``), or None to use
        # the camera default. When set, capture() resolves it to the resolution
        # and compression the profile implies and applies them to the snapshot.
        self._stream_id = stream_id
        # The camera session's store for resolved snapshot knobs (#69 CAM-P1),
        # keyed by stream id, so the StreamProfile group is read once per TTL
        # rather than on every capture. ``None``: read every time, as before.
        self._knob_cache = knob_cache
        # Called (on the event loop) once the event subscription is established
        # (#69 CAM-R7); set by the event-listener factory, never required.
        self.on_subscribed: Callable[[], Awaitable[None]] | None = None

    async def _stream_snapshot_knobs(
        self,
    ) -> tuple[str | None, int | None, str | None]:
        """Resolve the selected stream profile to its snapshot knobs.

        Returns ``(resolution, compression, degraded_reason)``. Reads the
        StreamProfile group (bounded in total by :data:`STREAM_PROFILE_TIMEOUT`),
        finds the profile whose name matches the configured ``stream_id``, and
        extracts the snapshot-relevant knobs from its parameters.

        A missing profile (renamed/removed on the camera), an unreadable group or
        a read that times out does not fail the capture: the adapter defaults are
        returned with a ``degraded_reason`` (one of the ``STREAM_PROFILE_*``
        notes), which the capture path reports as a warning event (owner
        decision OD-2). The switch to failing instead would be here: raise
        :class:`OtherCaptureError` in place of returning a reason.

        A clean resolution is kept in the shared knob cache, when there is one;
        a degraded one never is, so a profile that was briefly unreadable is
        retried on the next capture.
        """
        cache_key = ("stream_knobs", self._stream_id)
        if self._knob_cache is not None:
            cached = self._knob_cache.get(cache_key)
            if isinstance(cached, tuple) and len(cached) == 2:
                return cached[0], cached[1], None
        try:
            async with asyncio.timeout(STREAM_PROFILE_TIMEOUT):
                params = await self._read_params(STREAM_PROFILE_GROUP)
        except TimeoutError:
            degraded: str | None = STREAM_PROFILE_TIMED_OUT
        else:
            parameters = stream_profile_parameters(params, self._stream_id or "")
            if parameters is not None:
                resolution, compression = snapshot_knobs_from_parameters(parameters)
                # Fall back to the adapter defaults for any knob the profile omits.
                knobs = (
                    resolution if resolution is not None else self._resolution,
                    compression if compression is not None else self._compression,
                )
                if self._knob_cache is not None:
                    self._knob_cache.put(cache_key, knobs)
                return knobs[0], knobs[1], None
            degraded = STREAM_PROFILE_MISSING if params else STREAM_PROFILE_UNREADABLE
        # DEBUG: the capture path reports each reason once as a warning event.
        logger.debug(
            "vapix stream profile %r not applied (%s); using camera default",
            self._stream_id,
            degraded,
        )
        return self._resolution, self._compression, degraded

    async def capture(self) -> CapturedFrame:
        resolution, compression = self._resolution, self._compression
        degraded: str | None = None
        # Only touch the parameter CGI when a non-default stream is selected, so
        # a default-stream capture is byte-for-byte the single snapshot GET it
        # has always been (no extra request, identical URL).
        if self._stream_id is not None:
            resolution, compression, degraded = await self._stream_snapshot_knobs()
        url = build_snapshot_url(
            self._address,
            resolution=resolution,
            compression=compression,
            explicit_snapshot_uri=self._snapshot_uri,
        )
        try:
            image_bytes = await http_get_image(
                self._client,
                url,
                self._credentials,
                self._timeout,
                auth_state=self._auth_state,
            )
        except CaptureError:
            # Do not pin knobs a failed request was built from (a 404 after the
            # profile changed, say): the next capture re-reads them.
            if self._knob_cache is not None:
                self._knob_cache.drop(("stream_knobs", self._stream_id))
            raise
        frame = frame_from_bytes(image_bytes)
        if degraded is not None:
            frame.capture_notes.append(degraded)
        # Scene metadata is not read here: this call is bounded by the capture
        # budget, and a slow metadata read must never cost the frame (#69
        # CAM-R2). The capture path calls collect_scene_metadata afterwards.
        return frame

    async def collect_scene_metadata(
        self, frame: CapturedFrame
    ) -> dict[str, object] | None:
        """Best-effort scene-metadata snapshot for a just-captured frame.

        Performs a single short-timeout read of the camera's scene/image
        parameter group and returns a versioned envelope describing the effective
        scene settings, plus the frame's own captured resolution (free -- no
        extra request). The read is intentionally cheap and bounded:

        * exactly one parameter query, with a short dedicated per-request
          timeout; the caller adds a total timeout and runs this outside the
          capture budget;
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
                self._client,
                url,
                self._credentials,
                self._timeout,
                max_bytes=TEXT_RESPONSE_MAX_BYTES,
                auth_state=self._auth_state,
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
                self._client,
                url,
                self._credentials,
                self._timeout,
                max_bytes=TEXT_RESPONSE_MAX_BYTES,
                auth_state=self._auth_state,
            )
        except CaptureError as exc:
            # Keep ``local``: a pool timeout under the move is the server's
            # fault, not the camera's (#69 CAM-P2, #131).
            raise PTZError(f"PTZ move failed: {exc.message}", local=exc.local) from exc
        text = body.decode("utf-8", errors="replace")
        if is_ptz_error_body(text):
            raise PTZError(f"camera rejected PTZ move: {text.strip()}")
        # Give the physical dome time to arrive before the caller captures.
        await self._wait_until_settled()

    async def query_position(self) -> PtzPosition | None:
        """Read the dome's current pan/tilt/zoom, or ``None`` on any failure."""
        try:
            body = await http_get_image(
                self._client,
                build_ptz_position_url(self._address),
                self._credentials,
                self._timeout,
                max_bytes=TEXT_RESPONSE_MAX_BYTES,
                auth_state=self._auth_state,
            )
        except CaptureError as exc:
            logger.debug("vapix PTZ position query failed: %s", exc.message)
            return None
        return parse_ptz_position(body.decode("utf-8", errors="replace"))

    async def _wait_until_settled(self) -> None:
        """Wait until two consecutive position readings agree, bounded.

        Polls every :data:`PTZ_SETTLE_POLL_SECONDS`, waits at least
        :data:`PTZ_SETTLE_MIN_SECONDS` and at most :data:`PTZ_SETTLE_MAX_SECONDS`.
        A camera that does not report a position gets the fixed
        :data:`PTZ_SETTLE_SECONDS` pause instead, as before.
        """
        loop = asyncio.get_running_loop()
        started = loop.time()
        previous = await self.query_position()
        if previous is None:
            await asyncio.sleep(PTZ_SETTLE_SECONDS)
            return
        while loop.time() - started < PTZ_SETTLE_MAX_SECONDS:
            await asyncio.sleep(PTZ_SETTLE_POLL_SECONDS)
            current = await self.query_position()
            if current is None:
                # The position stopped reporting mid-settle: finish with the
                # fixed pause, less what has already been waited.
                elapsed = loop.time() - started
                await asyncio.sleep(max(0.0, PTZ_SETTLE_SECONDS - elapsed))
                return
            if current == previous and loop.time() - started >= PTZ_SETTLE_MIN_SECONDS:
                return
            previous = current
        logger.debug(
            "vapix PTZ position still changing after %.0fs; capturing anyway",
            PTZ_SETTLE_MAX_SECONDS,
        )

    async def _read_params(
        self, group: str, *, timeout: float | None = None
    ) -> dict[str, str]:
        """Fetch and parse a parameter group, returning {} on any failure.

        ``timeout`` overrides the adapter's default request timeout for this one
        read. The scene-metadata read on the capture hot path passes a short
        timeout so a slow camera can never eat the surrounding capture budget;
        all other callers fall back to ``self._timeout``. The body is capped at
        :data:`._bounded.TEXT_RESPONSE_MAX_BYTES`; an oversized group reads as a
        failure (``{}``).
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
                    max_bytes=TEXT_RESPONSE_MAX_BYTES,
                    auth_state=self._auth_state,
                )
            ).decode("utf-8", errors="replace")
        except CaptureError as exc:
            logger.debug("vapix param query failed for %s: %s", group, exc.message)
            return {}
        return parse_param_response(text)

    async def get_geolocation(self) -> GeoLocation | None:
        """Read the device position: the Geolocation API, then the legacy group.

        :raises GeolocationAccessDenied: when the Geolocation API refused this
            account (it requires an Administrator) and the legacy parameter group
            produced no location either. A device that is reachable but has no
            position configured still returns None.
        """
        # Current Axis firmware serves the location from the dedicated
        # Geolocation API; query it first.
        denied: AuthCaptureError | None = None
        try:
            location = await self._read_geolocation_api()
        except AuthCaptureError as exc:
            denied = exc
            location = None
        if location is not None:
            return location
        # Fall back to the legacy parameter group for older devices that predate
        # the API (the API answers with an error there).
        params = await self._read_params("Geolocation")
        location = geolocation_from_params(params)
        if location is None and denied is not None:
            raise GeolocationAccessDenied(
                protocol="vapix",
                required_role=GEOLOCATION_REQUIRED_ROLE,
                status=_auth_status(denied),
            ) from denied
        return location

    async def _read_geolocation_api(self) -> GeoLocation | None:
        """Query the Axis Geolocation API and parse its position document.

        Best-effort for everything except an auth refusal: an unreachable camera
        or a non-success response (e.g. the device lacks the API) degrades to
        None so the caller can fall back to the legacy parameter group.

        :raises AuthCaptureError: when the device refused the account (401/403).
            Axis serves this API to Administrators only, so the refusal is logged
            at INFO -- it is actionable, not noise -- and re-raised for
            :meth:`get_geolocation` to turn into a typed denial. The log names the
            device and the status, never the account or its password.
        """
        try:
            return await self.read_geolocation_api()
        except AuthCaptureError:
            raise
        except CaptureError as exc:
            logger.debug("vapix geolocation query failed: %s", exc.message)
            return None

    async def read_geolocation_api(self) -> GeoLocation | None:
        """Read only the Geolocation API (``get.cgi``), raising on any failure.

        Unlike :meth:`get_geolocation` there is no legacy fallback and nothing is
        swallowed, so returning at all proves ``get.cgi`` answered 2xx with this
        adapter's credentials. The one-time admin read relies on that to tell an
        accepted administrator login from any other outcome.

        Returns the parsed position, or None when the device answered but has no
        position set.

        :raises AuthCaptureError: when the device refused the account (401/403),
            logged at INFO with the device and status only -- never the account
            or its password.
        :raises CaptureError: on any other reachability or status failure.
        """
        base_url = _base_url(self._address)
        url = f"{base_url}{GEOLOCATION_PATH}"
        try:
            body = await http_get_image(
                self._client,
                url,
                self._credentials,
                self._timeout,
                max_bytes=TEXT_RESPONSE_MAX_BYTES,
                auth_state=self._auth_state,
            )
        except AuthCaptureError as exc:
            logger.info(
                "vapix geolocation read denied by %s (status %s): get.cgi requires "
                "an %s account",
                base_url,
                _auth_status(exc),
                GEOLOCATION_REQUIRED_ROLE,
            )
            raise
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

    async def _guard_event_url(self, url: str) -> None:
        """Validate an event URL against the camera deny-list, fail-closed.

        Camera/scan policy (admin private opt-in honoured; loopback/link-local/
        metadata never relaxed). A denied target surfaces as
        :class:`UnreachableCaptureError`.

        This is the early, readable guard for the SOAP enumeration path, which
        rides the camera ``httpx`` client. That client's transport dials only
        SSRF-validated addresses (see :mod:`.clients`), as the raw-socket WS path
        does through :meth:`_pinned_event_host`; the lookup here runs off the
        event loop and its cached answer is the one the connection uses.
        """
        try:
            await check_camera_url_async(url)
        except SsrfError as exc:
            raise UnreachableCaptureError(f"event URL blocked: {exc}") from exc

    async def _pinned_event_host(self, url: str) -> str:
        """Resolve+validate the event URL host, returning a validated IP to dial.

        The raw-socket event path connects by IP (rather than re-resolving the
        hostname) so a DNS rebind cannot redirect the connection to a denied
        address after the deny-list check passes. The original hostname is still
        presented as SNI / ``Host`` by the connection itself. Uses camera/scan
        policy (admin private opt-in honoured; loopback/link-local/metadata never
        relaxed). A denied target or a resolution failure surfaces as
        :class:`UnreachableCaptureError` so it flows through the listener's
        existing recoverable-failure handling.
        """
        from ..runtime import get_context

        ssrf = get_context().settings.ssrf
        host = urlsplit(url).hostname or ""
        try:
            return await resolve_and_check_pinned_async(
                host,
                allow_private=True,
                allowed_private_subnets=ssrf.allowed_private_subnets,
            )
        except SsrfError as exc:
            raise UnreachableCaptureError(f"event URL blocked: {exc}") from exc
        except OSError as exc:
            # socket.gaierror (an OSError) -- the host did not resolve, so the
            # endpoint is unreachable for this attempt; let the loop back off.
            raise UnreachableCaptureError(
                f"event URL host {host!r} did not resolve: {exc}"
            ) from exc

    async def list_event_topics(self) -> list[EventDescriptor]:
        # Query GetEventInstances over the guarded HTTP path (plain digest, the
        # same realm snapshot uses). Every failure mode degrades to an empty list
        # so a caller can enumerate inline without a try/except.
        url = f"{_base_url(self._address)}{VAPIX_SERVICES_PATH}"
        try:
            await self._guard_event_url(url)
            text = await self._post_soap(url, get_event_instances_body())
        except CaptureError as exc:
            logger.debug("vapix event-instance enumeration failed: %s", exc.message)
            return []
        return parse_event_instances(text)

    async def _post_soap(self, url: str, body: str) -> str:
        """POST a SOAP body, handling a digest challenge, returning the text.

        Mirrors :func:`http_get_image`'s negotiation for a POST: the scheme the
        adapter's auth state remembers goes up front, and a 401 is answered from
        its own challenge, once (#69 CAM-S1). The body is streamed and capped at
        :data:`._bounded.SOAP_RESPONSE_MAX_BYTES` (#69 CAM-S3). Maps
        transport/timeout/auth/status/size failures to :class:`CaptureError` so
        the caller's recoverable-failure handling applies.
        """
        from .http_jpeg import classify_http_status

        content_type = {"Content-Type": "application/soap+xml; charset=utf-8"}
        per_request = request_timeout(self._client, self._timeout)
        credentials = self._credentials
        state = self._auth_state
        try:
            preemptive = state.preemptive("POST", url, credentials)
            headers = dict(content_type)
            if preemptive:
                headers["Authorization"] = preemptive
            response = await read_capped_response(
                self._client,
                "POST",
                url,
                max_bytes=SOAP_RESPONSE_MAX_BYTES,
                content=body,
                headers=headers,
                timeout=per_request,
            )
            if response.status_code == 401 and credentials is not None:
                answer = state.respond(
                    response.headers.get_list("www-authenticate"),
                    "POST",
                    url,
                    credentials,
                )
                response = await read_capped_response(
                    self._client,
                    "POST",
                    url,
                    max_bytes=SOAP_RESPONSE_MAX_BYTES,
                    content=body,
                    headers={**content_type, "Authorization": answer},
                    timeout=per_request,
                )
                if response.status_code == 401:
                    state.forget(url)
        except httpx.PoolTimeout as exc:
            # Before TimeoutException (its base): no free local connection is
            # not a camera failure (#69 CAM-P2).
            raise pool_exhausted_error(url) from exc
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
        connect_host = await self._pinned_event_host(url)
        connection = await _ws_connect(
            self._client,
            url,
            connect_host,
            self._credentials,
            self._timeout,
            _resolve_ws_limits(),
            self._auth_state,
            contain_default=self._contain_default,
        )
        try:
            # Bound the subscribe write so a camera whose socket wedges the drain
            # cannot hang the listener before the first receive() applies its own
            # idle timeout.
            async with asyncio.timeout(self._timeout):
                await connection.send_text(ws_subscribe_message())
            if self.on_subscribed is not None:
                await self.on_subscribed()
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
