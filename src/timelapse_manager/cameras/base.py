"""The camera-adapter seam: the abstract interface plus the typed results and
errors every concrete adapter and every consumer of an adapter depends on.

A :class:`CameraAdapter` knows how to talk to one camera over one protocol. The
capture engine builds an adapter (via the factory in :mod:`.registry`), calls
its async methods, and never reaches into protocol-specific details. Keeping the
result and error shapes here means the engine and the adapters agree on a single
vocabulary for success (:class:`CapturedFrame`) and failure
(:class:`ValidationFailure` / :class:`CaptureError`).

Adapters are import-safe and construction-safe: importing this package or
building an adapter performs no network or subprocess work. Side effects happen
only when an ``async`` method is called.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


@dataclass
class CapturedFrame:
    """One still image grabbed from a camera.

    :param image_bytes: the raw encoded image (typically JPEG).
    :param width: pixel width parsed from the image.
    :param height: pixel height parsed from the image.
    :param format: lower-case container/codec name, e.g. ``"jpeg"``.
    :param captured_at: timezone-aware UTC instant the frame was obtained.
    :param scene_metadata: an optional best-effort snapshot of the camera's
        scene/image settings at the moment of capture (brightness, contrast,
        exposure, and so on), as a small JSON-serialisable envelope. ``None``
        when the protocol exposes no such data or the read failed; collecting it
        must never fail or delay a capture, so its absence is always benign. This
        is the carrier from the adapter to the frame writer, which persists it
        alongside the frame row.
    """

    image_bytes: bytes
    width: int
    height: int
    format: str
    captured_at: datetime
    scene_metadata: dict | None = None


class ValidationFailure(str, Enum):  # noqa: UP042 - stable str-mixin enum
    """The classified reason a connection test or capture failed.

    A ``str``-mixin enum (rather than :class:`enum.StrEnum`) so the value both
    *is* a ``str`` and serialises cleanly, while keeping a member's ``repr`` and
    cross-version behaviour predictable for the consumers that depend on this
    shape.
    """

    AUTH = "auth"
    UNREACHABLE = "unreachable"
    TIMEOUT = "timeout"
    UNSUPPORTED_CODEC = "unsupported_codec"
    OTHER = "other"


@dataclass
class ValidationResult:
    """Outcome of :meth:`CameraAdapter.validate_connection`.

    :param ok: True when the camera was reached and accepted the request.
    :param reason: the failure category when ``ok`` is False, else None.
    :param message: a short human-readable explanation, safe to surface.
    """

    ok: bool
    reason: ValidationFailure | None
    message: str


@dataclass
class GeoLocation:
    """A camera's geographic position.

    :param latitude: decimal degrees, north positive.
    :param longitude: decimal degrees, east positive.
    :param source: ``"camera"`` if reported by the device, ``"manual"`` if set
        by an operator override.
    """

    latitude: float
    longitude: float
    source: str


@dataclass
class CameraCapabilities:
    """What a camera supports, as far as the adapter can determine.

    :param supported_resolutions: resolution strings such as ``"1920x1080"``;
        may be empty when the protocol exposes no capability query.
    :param compression_range: inclusive ``(min, max)`` compression levels when
        the protocol exposes them, else None.
    """

    supported_resolutions: list[str]
    compression_range: tuple[int, int] | None = None


@dataclass
class StreamProfile:
    """One named stream/profile a camera can be captured from.

    :param id: the stable identifier the adapter uses to select this stream on a
        later capture (e.g. an Axis stream-profile name or an ONVIF profile
        token). Round-trips verbatim: an ``id`` returned here is exactly what a
        caller stores and hands back to select this stream.
    :param label: a human-readable name for the stream, safe to show in a UI.
    """

    id: str
    label: str


@dataclass
class StreamProfileResult:
    """Outcome of :meth:`CameraAdapter.list_stream_profiles`.

    Carries the enumerated profiles plus a clean reachable/empty signal so a
    caller can render an inline error instead of catching an exception: listing
    profiles never raises for an ordinary connection or parse problem, it reports
    the outcome here.

    :param profiles: the streams found; empty when ``ok`` is False, and may also
        be empty when the camera was reached but exposes no selectable streams.
    :param ok: True when the camera was reached and its streams were read (or the
        adapter has a single implicit stream); False when it could not be reached
        or its response could not be parsed.
    :param message: a short human-readable explanation when ``ok`` is False (safe
        to surface), else None.
    """

    profiles: list[StreamProfile]
    ok: bool = True
    message: str | None = None


@dataclass(frozen=True)
class PTZPreset:
    """One named pan/tilt/zoom preset position a camera can be sent to.

    :param id: the stable identifier a caller hands back to recall this preset
        (for Axis this is the preset's name, the token its goto command expects).
    :param label: a human-readable name for the preset, safe to show in a UI.
    """

    id: str
    label: str


@dataclass(frozen=True)
class PTZPresetsResult:
    """Outcome of :meth:`CameraAdapter.list_ptz_presets`.

    Carries the enumerated presets plus a clean supported/reachable signal so a
    caller can render an inline state instead of catching an exception:
    enumerating presets never raises for an ordinary reachability or parse
    problem, it reports the outcome here (mirroring :class:`StreamProfileResult`).

    :param presets: the presets found; empty when the camera exposes none, or
        when ``ok`` is False.
    :param ptz_supported: True when the camera/adapter can be positioned (it has
        a PTZ mechanism), False for a fixed camera or an adapter without PTZ.
    :param ok: True when the enumeration completed (the camera was reached and
        its response parsed); False when it could not be reached or parsed.
    :param message: a short human-readable explanation when ``ok`` is False (safe
        to surface), else None.
    """

    presets: list[PTZPreset]
    ptz_supported: bool
    ok: bool
    message: str | None = None


@dataclass
class DiscoveredCamera:
    """A camera found by discovery, before it is configured/persisted.

    Snapshot/stream URIs are often unknown at discovery time (they are resolved
    later by the ONVIF adapter), so they are optional.

    :param address: the host (IP or name) the camera was found at.
    :param protocol: the protocol family it speaks, e.g. ``"onvif"``.
    :param snapshot_uri: a snapshot URL if discovery surfaced one, else None.
    :param stream_uri: an RTSP URL if discovery surfaced one, else None.
    :param geolocation: device-reported location if available, else None.
    :param vendor: a vendor/model hint if advertised, else None.
    """

    address: str
    protocol: str
    snapshot_uri: str | None
    stream_uri: str | None
    geolocation: GeoLocation | None
    vendor: str | None


@dataclass
class EventDescriptor:
    """One event topic a camera can emit, as discovered at configuration time.

    The descriptor is what an operator picks from to build a trigger and what the
    runtime matches an incoming notification against, so its identity lives in
    :attr:`topic_id` -- the canonical, prefix-stripped, ``/``-joined topic path
    (e.g. ``Device/IO/VirtualInput``). That canonical form is shared across both
    protocols and across the discovery-vs-live dialect difference, so a trigger
    stored against it matches a live notification regardless of which namespace
    prefixes the camera decorates the topic with.

    :param topic_id: the canonical topic key (prefix-stripped path); the stored
        trigger selector and the runtime match key.
    :param raw_topic: the topic string exactly as the camera advertised it
        (carries namespace prefixes); kept for debugging/diagnostics only.
    :param label: a human-readable name for the event, safe to show in a UI.
    :param category: a coarse grouping, one of ``motion``, ``tamper``,
        ``analytics``, ``io``, ``scene``, or ``other``.
    :param stateful: True when the event carries a boolean state that asserts and
        clears (so a trigger should fire on the rising edge, not on clear); False
        for a stateless one-shot pulse.
    :param data_fields: the names of the data/source fields the event carries,
        each as ``{"name": str, "type": str}`` (``type`` is ``"boolean"`` or
        ``"string"``); empty when the protocol exposes no field list.
    :param protocol: the protocol the descriptor was discovered over (``"onvif"``
        or ``"vapix"``); defaulted so a descriptor can be built from the named
        fields alone.
    :param requires_app: True when the event depends on an optional on-camera
        analytics application (e.g. object analytics) and so may be present in the
        catalogue but never fire without that app installed.
    """

    topic_id: str
    raw_topic: str
    label: str
    category: str
    stateful: bool
    data_fields: list[dict[str, str]] = field(default_factory=list)
    protocol: str = ""
    requires_app: bool = False


@dataclass
class CameraEvent:
    """One normalised event notification received from a camera.

    Both transports (ONVIF PullPoint, VAPIX WebSocket) collapse to this single
    shape so the matching layer is protocol-agnostic. The identity is the
    canonical :attr:`topic_id`, compared against a configured trigger's stored
    topic; :attr:`active` carries the normalised rising-edge state so the matcher
    can fire on assertion and ignore the clear.

    :param topic_id: the canonical topic key (prefix-stripped path), derived from
        the raw notification topic the same way the descriptor's is, so a live
        event matches the trigger stored against the descriptor.
    :param category: the coarse grouping (see :class:`EventDescriptor`).
    :param source: the notification's ``Source`` SimpleItems as a name->value map
        (strings), identifying *which* instance fired (e.g. ``{"port": "1"}``).
    :param data: the notification's ``Data`` SimpleItems as a name->value map
        (strings), carrying the event payload (e.g. ``{"active": "1"}``).
    :param active: the normalised rising-edge state -- True on assertion, False on
        clear, ``None`` for a stateless event or one with no recognised state
        field. The matcher fires a stateful trigger only when this is True.
    :param occurred_at: the timezone-aware UTC instant the camera stamped on the
        notification.
    :param raw: the raw notification material (the SimpleItem maps merged, plus
        any transport-specific extras); for debugging/diagnostics only.
    """

    topic_id: str
    category: str
    source: dict[str, str]
    data: dict[str, str]
    active: bool | None
    occurred_at: datetime
    raw: dict[str, object] = field(default_factory=dict)


class EventNotSupportedError(Exception):
    """The camera/adapter exposes no usable event mechanism.

    Raised by the event-source openers and topic enumerators when a protocol or a
    particular firmware does not support events at all (e.g. a VAPIX WebSocket
    endpoint that answers ``404`` on older firmware), so a caller can fall back to
    another transport or degrade to "no live events" rather than treating it as a
    capture failure.
    """


class CaptureError(Exception):
    """Base class for a failed capture, carrying a classified reason.

    Concrete subclasses fix :attr:`reason` so callers can branch on the failure
    category without string-matching the message. Prefer raising a subclass.
    """

    #: Default reason; overridden by subclasses.
    reason: ValidationFailure = ValidationFailure.OTHER

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class AuthCaptureError(CaptureError):
    """Authentication was rejected (bad or missing credentials)."""

    reason = ValidationFailure.AUTH


class UnreachableCaptureError(CaptureError):
    """The camera host could not be reached (DNS, connection refused, etc.)."""

    reason = ValidationFailure.UNREACHABLE


class TimeoutCaptureError(CaptureError):
    """The camera did not respond within the allotted time."""

    reason = ValidationFailure.TIMEOUT


class UnsupportedCodecCaptureError(CaptureError):
    """The stream used a codec or container the pipeline cannot decode."""

    reason = ValidationFailure.UNSUPPORTED_CODEC


class OtherCaptureError(CaptureError):
    """An uncategorised failure not covered by the more specific subclasses."""

    reason = ValidationFailure.OTHER


class PTZError(CaptureError):
    """A pan/tilt/zoom positioning request failed.

    Raised fail-closed: a camera left pointing the wrong way is worse than a
    missed frame, so callers should treat this as a hard stop and not capture
    from a position they could not confirm. Covers a rejected goto/move command
    as well as the reachability/auth failures underneath it.
    """


class PTZUnsupportedError(PTZError):
    """The camera or adapter has no pan/tilt/zoom mechanism to position.

    A subclass of :class:`PTZError` so a caller that fails closed on any
    positioning error catches this too, while a caller that wants to distinguish
    "cannot move" from "move failed" can branch on the specific type.
    """


# The implicit single stream every adapter exposes when it has no concept of
# selectable streams (rtsp/http) or cannot enumerate them. Selecting it is
# equivalent to selecting nothing -- the adapter captures from its one stream.
DEFAULT_STREAM_ID = "default"
DEFAULT_STREAM_LABEL = "Default stream"


class CameraAdapter(ABC):
    """Abstract driver for one camera over one protocol.

    Implementations must keep ``__init__`` free of I/O. All real work happens in
    the async methods below, and :meth:`close` must be safe to call more than
    once.
    """

    async def list_stream_profiles(self) -> StreamProfileResult:
        """Enumerate the camera's selectable streams/profiles.

        The default returns a single implicit "default" stream, which is correct
        for any adapter that exposes exactly one stream (rtsp/http) -- they need
        no override. Adapters whose protocol advertises multiple streams (Axis
        VAPIX, ONVIF) override this to enumerate them.

        Never raises for an ordinary reachability or parse problem; the outcome
        is reported in the returned :class:`StreamProfileResult` (``ok`` False
        with a message) so a caller can surface it inline and never crash.
        """
        return StreamProfileResult(
            profiles=[StreamProfile(id=DEFAULT_STREAM_ID, label=DEFAULT_STREAM_LABEL)],
            ok=True,
            message=None,
        )

    async def list_ptz_presets(self) -> PTZPresetsResult:
        """Enumerate the camera's named pan/tilt/zoom preset positions.

        The default reports no PTZ support and no presets, which is correct for
        any fixed camera or any adapter whose protocol has no positioning concept
        (rtsp/http/onvif inherit it). Adapters backed by a PTZ-capable protocol
        (Axis VAPIX) override this to enumerate the camera's presets.

        Never raises for an ordinary reachability or parse problem; the outcome
        is reported in the returned :class:`PTZPresetsResult` (``ok`` False with a
        message) so a caller can surface it inline and never crash.
        """
        return PTZPresetsResult(presets=[], ptz_supported=False, ok=True, message=None)

    async def move_to(
        self,
        *,
        preset_id: str | None = None,
        pan: float | None = None,
        tilt: float | None = None,
        zoom: float | None = None,
    ) -> None:
        """Position the camera, by named preset or by raw pan/tilt/zoom.

        Pass either ``preset_id`` (recall a saved position) or any combination of
        ``pan``/``tilt``/``zoom`` (an absolute move in the camera's own units).
        Calling with no arguments is a no-op: there is nothing to position to.

        The default supports neither: with no arguments it returns, and with any
        positioning argument it raises :class:`PTZUnsupportedError`, so a fixed
        camera or a non-PTZ adapter (rtsp/http/onvif) inherits a safe surface that
        refuses to silently ignore a real positioning request. PTZ-capable
        adapters override this.

        :raises PTZError: (fail-closed) when a positioning request cannot be
            satisfied -- including :class:`PTZUnsupportedError` when the camera or
            adapter has no PTZ mechanism.
        """
        if preset_id is None and pan is None and tilt is None and zoom is None:
            return
        raise PTZUnsupportedError("this camera does not support pan/tilt/zoom")

    async def list_event_topics(self) -> list[EventDescriptor]:
        """Enumerate the event topics this camera can emit.

        Used at configuration time to populate the set of events an operator can
        build a trigger from. The default reports none, which is correct for any
        adapter whose protocol has no event concept (rtsp/http inherit it).
        Event-capable adapters (ONVIF, Axis VAPIX) override this to query the
        device's topic catalogue and return canonical descriptors.

        Never raises for an ordinary reachability or parse problem -- it reports
        "no topics" as an empty list -- so a caller can enumerate without a
        try/except. A protocol with no event support at all returns an empty list
        rather than raising :class:`EventNotSupportedError`.
        """
        return []

    def open_event_source(self) -> AsyncIterator[CameraEvent]:
        """Return an async iterator that yields live :class:`CameraEvent`s.

        The returned iterator owns its own subscription lifecycle: iterating it
        subscribes (or upgrades a connection), long-polls/streams the camera, and
        yields one canonical :class:`CameraEvent` per notification until the
        consumer stops iterating, at which point it tears the subscription down.
        Construction is I/O-free -- the subscribe happens on first iteration -- so
        a caller can build the source cheaply and decide later whether to drive
        it. ``CancelledError`` raised into the iterator (on consumer teardown)
        propagates after a best-effort unsubscribe.

        The default raises :class:`EventNotSupportedError`, which is correct for
        any adapter whose protocol has no event mechanism (rtsp/http inherit it).
        Event-capable adapters (ONVIF, Axis VAPIX) override this.

        :raises EventNotSupportedError: when the adapter exposes no event source.
        """
        raise EventNotSupportedError("this adapter exposes no event source")

    @abstractmethod
    async def capture(self) -> CapturedFrame:
        """Grab a single frame.

        :raises CaptureError: (a subclass) when the frame cannot be obtained.
        """
        raise NotImplementedError

    @abstractmethod
    async def validate_connection(self) -> ValidationResult:
        """Test reachability and authentication without persisting anything.

        Never raises for an ordinary connection problem; it reports the outcome
        in the returned :class:`ValidationResult`.
        """
        raise NotImplementedError

    @abstractmethod
    async def get_geolocation(self) -> GeoLocation | None:
        """Return the camera's geolocation if available, else None."""
        raise NotImplementedError

    async def get_device_hostname(self) -> str | None:
        """Return the camera's network hostname if it reports one, else None.

        The default returns None, which is correct for any protocol that exposes
        no hostname query (rtsp/http inherit it). Adapters whose protocol can
        report a device hostname (Axis VAPIX, ONVIF) override this. Like the other
        best-effort metadata reads, it must never raise for an ordinary
        reachability or parse problem: it reports "no hostname" as None.
        """
        return None

    @abstractmethod
    async def capabilities(self) -> CameraCapabilities:
        """Return the camera's reported capabilities."""
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        """Release any resources held by the adapter. Idempotent."""
        raise NotImplementedError
