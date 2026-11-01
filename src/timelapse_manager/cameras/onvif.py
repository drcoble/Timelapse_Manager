"""ONVIF adapter.

ONVIF cameras do not serve a snapshot directly at a fixed path; instead the
client asks the device's Media service for the snapshot and stream URIs of a
media profile, then fetches from those. This adapter performs that resolution
over SOAP (see :mod:`._onvif_soap`) and then delegates the actual grab to the
HTTP/JPEG helper (for the snapshot URI) or the RTSP adapter (for the stream
URI), so the protocol-specific capture logic lives in one place each.

Resolved URIs are cached on the instance after the first lookup so repeated
captures do not re-query the device.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx

from ..security.credential_binding import split_userinfo, target_host
from ..security.ssrf import SsrfError, pinned_addresses_async
from . import _onvif_soap as soap
from . import events as eventutil
from ._bounded import SOAP_RESPONSE_MAX_BYTES, read_capped_response
from .base import (
    AdapterCache,
    AuthCaptureError,
    CameraAdapter,
    CameraCapabilities,
    CameraEvent,
    CapturedFrame,
    CaptureError,
    EventDescriptor,
    GeoLocation,
    OtherCaptureError,
    StreamProfile,
    StreamProfileResult,
    TimeoutCaptureError,
    UnreachableCaptureError,
    ValidationResult,
    valid_coordinates,
)
from .clients import pool_exhausted_error, request_timeout
from .host_resolution import check_camera_url_async
from .http_auth import CameraAuthState
from .http_jpeg import frame_from_bytes, http_get_image
from .rtsp import RtspAdapter, rtsp_capture_budget_seconds
from .rtsp import _guard_stream_url as _guard_stream_uri

logger = logging.getLogger(__name__)

DEVICE_SERVICE_PATH = "/onvif/device_service"
# The shared services endpoint the event operations target on this firmware. The
# real address is discovered from GetServices (the Events service XAddr); this is
# the conventional fallback when discovery is unavailable.
EVENTS_SERVICE_PATH = "/onvif/services"

_GET_PROFILES_BODY = "<trt:GetProfiles/>"

# SOAP fault subcodes a device returns when the WS-Security token is refused:
# ONVIF's ``ter:NotAuthorized`` and WS-Security's ``wsse:FailedAuthentication``.
_AUTH_FAULT_MARKERS = ("NotAuthorized", "FailedAuthentication")
# Statuses a SOAP fault arrives with: 400 for a sender fault per the SOAP 1.2
# HTTP binding, 500 on firmware that answers every fault with it.
_FAULT_STATUSES = frozenset({400, 500})


def soap_auth_failure(status_code: int, text: str) -> bool:
    """Whether a failed SOAP response means the camera refused the login.

    ``401``/``403`` always do; a ``400``/``500`` SOAP fault does when its body
    names an authentication subcode (#69 CAM-R9). The caller then raises
    :class:`AuthCaptureError`, which never falls back to RTSP.
    """
    if status_code in (401, 403):
        return True
    return status_code in _FAULT_STATUSES and any(
        marker in text for marker in _AUTH_FAULT_MARKERS
    )


def _raise_for_soap_status(status_code: int, text: str, url: str, what: str) -> None:
    """Map a failed SOAP response to the matching capture error, else return."""
    if status_code < 400:
        return
    if soap_auth_failure(status_code, text):
        raise AuthCaptureError(
            f"authentication rejected ({status_code}) by {what} to {url}"
        )
    raise OtherCaptureError(f"{what} to {url} returned {status_code}")


_GET_SERVICES_BODY = (
    "<tds:GetServices><tds:IncludeCapability>false"
    "</tds:IncludeCapability></tds:GetServices>"
)

# How long to hold a PullMessages long-poll open, and the matching httpx read
# budget (the request must outlast the server-side hold or it times out every
# cycle). Renew is sent when the subscription is within this margin of expiry.
_PULL_TIMEOUT = "PT15S"
_PULL_TIMEOUT_SECONDS = 15
_PULL_HTTP_TIMEOUT = 25.0
_SUBSCRIPTION_TERMINATION = "PT300S"
_SUBSCRIPTION_TERMINATION_SECONDS = 300
_RENEW_MARGIN_SECONDS = 60
# A short, bounded budget for the best-effort Unsubscribe on teardown so a slow
# camera cannot stall shutdown.
_UNSUBSCRIBE_TIMEOUT = 5.0
# The shortest a PullMessages cycle may take. A camera that answers an empty pull
# at once (some firmware ignores the long-poll timeout) would otherwise be polled
# in a tight loop, hammering it and the event loop (#69 CAM-R9).
_MIN_PULL_CYCLE_SECONDS = 1.0
# The camera's clock offset is kept in the session's cache under this key, as
# ``(offset_seconds, measured_at_monotonic)``. An authentication failure re-measures
# it only when the stored value is older than the refresh interval, so a camera
# that rejects the login for any other reason is not asked for its clock each time.
_CLOCK_OFFSET_KEY = ("clock_offset", None)
_CLOCK_OFFSET_REFRESH_SECONDS = 300.0
# Offsets within this many seconds are measurement noise (the camera reports whole
# seconds, truncated) and are treated as none.
_CLOCK_OFFSET_NOISE_SECONDS = 1
# Offsets beyond this are ignored.
_CLOCK_OFFSET_MAX_SECONDS = 86400


def _device_url(address: str) -> str:
    """Return the ONVIF device service URL for an address."""
    if address.startswith(("http://", "https://")):
        base = address.rstrip("/")
        if base.endswith(DEVICE_SERVICE_PATH):
            return base
        return f"{base}{DEVICE_SERVICE_PATH}"
    return f"http://{address}{DEVICE_SERVICE_PATH}"


def get_snapshot_uri_body(profile_token: str) -> str:
    """SOAP body requesting the snapshot URI for a media profile.

    The token comes from the device (or the operator), so it is XML-escaped.
    """
    return (
        "<trt:GetSnapshotUri>"
        f"<trt:ProfileToken>{escape(profile_token)}</trt:ProfileToken>"
        "</trt:GetSnapshotUri>"
    )


def get_stream_uri_body(profile_token: str) -> str:
    """SOAP body requesting the RTSP stream URI for a media profile.

    The token comes from the device (or the operator), so it is XML-escaped.
    """
    return (
        "<trt:GetStreamUri>"
        "<trt:StreamSetup>"
        "<tt:Stream xmlns:tt='http://www.onvif.org/ver10/schema'>"
        "RTP-Unicast</tt:Stream>"
        "<tt:Transport xmlns:tt='http://www.onvif.org/ver10/schema'>"
        "<tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
        "</trt:StreamSetup>"
        f"<trt:ProfileToken>{escape(profile_token)}</trt:ProfileToken>"
        "</trt:GetStreamUri>"
    )


def parse_first_profile_token(xml_text: str) -> str | None:
    """Return the first profile token from a GetProfilesResponse, or None."""
    root = soap.parse_xml(xml_text)
    if root is None:
        return None
    profile = root.find(".//trt:Profiles", soap.NS)
    if profile is None:
        return None
    token = profile.get("token")
    return token or None


def parse_profiles(xml_text: str) -> list[StreamProfile]:
    """Return all media profiles from a GetProfilesResponse as (token, name).

    Each ``trt:Profiles`` element carries the profile token in its ``token``
    attribute and, usually, a human name in a ``tt:Name`` child. The token is the
    stable :attr:`StreamProfile.id` -- exactly the value the capture path consumes
    to target this profile. When a profile has no name, its token doubles as the
    label so the entry is still usable. A profile with no token is skipped.
    """
    root = soap.parse_xml(xml_text)
    if root is None:
        return []
    profiles: list[StreamProfile] = []
    for element in root.findall(".//trt:Profiles", soap.NS):
        token = element.get("token")
        if not token:
            continue
        name = soap.find_text(element, "tt:Name") or token
        profiles.append(StreamProfile(id=token, label=name))
    return profiles


def _find_text_local(root: object, local_name: str) -> str | None:
    """Return stripped text of the first element with this local (unqualified) name.

    A namespace-agnostic fallback for elements whose namespace varies across
    firmware (e.g. the hostname ``Name``, emitted by some devices in the ``tds``
    namespace rather than ``tt``). Iterates the tree comparing the tag's local
    part after stripping any ``{namespace}`` prefix.
    """
    if not isinstance(root, ET.Element):
        return None
    for element in root.iter():
        tag = element.tag
        if (
            isinstance(tag, str)
            and tag.rsplit("}", 1)[-1] == local_name
            and element.text is not None
        ):
            text = element.text.strip()
            if text:
                return text
    return None


def _parse_events_xaddr(xml_text: str) -> str | None:
    """Return the Events service XAddr from a GetServicesResponse, or None.

    GetServices lists each service as a ``tds:Service`` with a ``Namespace`` and
    an ``XAddr``. The events service is the one whose namespace is the ONVIF
    events WSDL; its XAddr is where the event operations are POSTed. A response
    that omits the events service (or cannot be parsed) yields None so the caller
    falls back to the conventional path.
    """
    root = soap.parse_xml(xml_text)
    if root is None:
        return None
    events_ns = soap.NS["tev"]
    for service in root.iter():
        if service.tag.rsplit("}", 1)[-1] != "Service":
            continue
        namespace = soap.find_text(service, "tds:Namespace") or soap._find_local_text(
            service, "Namespace"
        )
        if namespace == events_ns:
            xaddr = soap.find_text(service, "tds:XAddr") or soap._find_local_text(
                service, "XAddr"
            )
            return xaddr
    return None


def _descriptor_for(topic_id: str, raw_topic: str) -> EventDescriptor:
    """Build an :class:`EventDescriptor` for a canonical ONVIF topic.

    ONVIF's GetEventProperties on this firmware does not inline the per-topic
    field list for most topics, so ``data_fields`` is left empty here (VAPIX's
    GetEventInstances is the richer field source); ``stateful`` is inferred from
    the topic family (the property/state topics that carry a rising edge).
    """
    category = eventutil.category_for_topic(topic_id)
    stateful = _is_stateful_topic(topic_id, category)
    requires_app = category == "analytics"
    return EventDescriptor(
        topic_id=topic_id,
        raw_topic=raw_topic,
        label=eventutil.label_for_topic(topic_id),
        category=category,
        stateful=stateful,
        data_fields=[],
        protocol="onvif",
        requires_app=requires_app,
    )


def _is_stateful_topic(topic_id: str, category: str) -> bool:
    """Infer whether an ONVIF topic carries an asserting/clearing state.

    The motion, io, tamper, and scene families on this vendor are property
    (rising-edge) topics; stateless one-shot families (audit logs, network
    add/remove) fall outside them. Analytics is stateful when it reports an
    ``active`` flag, which this vendor's ObjectAnalytics does.
    """
    return category in ("motion", "io", "tamper", "scene", "analytics")


def _event_from_notification(notification: dict[str, object]) -> CameraEvent | None:
    """Turn a parsed PullMessages notification into a canonical event.

    Returns None for a notification with no topic (nothing to match). The topic
    is canonicalised, source/data SimpleItems are merged to resolve the
    rising-edge ``active`` state, and the camera's ``UtcTime`` is parsed (falling
    back to now on an absent/unparseable stamp).
    """
    raw_topic = str(notification.get("topic") or "")
    if not raw_topic:
        return None
    topic_id = eventutil.canonicalize_topic(raw_topic)
    if not topic_id:
        return None
    source = _as_str_map(notification.get("source"))
    data = _as_str_map(notification.get("data"))
    category = eventutil.category_for_topic(topic_id)
    stateful = notification.get("operation") is not None
    attrs = {**source, **data}
    active = eventutil.normalize_active(attrs, stateful=stateful)
    occurred_at = _parse_utc_time(notification.get("utc_time"))
    return CameraEvent(
        topic_id=topic_id,
        category=category,
        source=source,
        data=data,
        active=active,
        occurred_at=occurred_at,
        raw={"topic": raw_topic, "operation": notification.get("operation")},
    )


def _as_str_map(value: object) -> dict[str, str]:
    """Coerce a parsed SimpleItem map to ``dict[str, str]``."""
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


def _parse_utc_time(value: object) -> datetime:
    """Parse an ONVIF ``UtcTime`` attribute to aware-UTC, else now.

    Accepts the ISO-8601 form ONVIF stamps (``...Z`` or with an offset). An
    absent or unparseable value falls back to the current instant so an event is
    never dropped for a malformed timestamp.
    """
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return datetime.now(UTC)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return datetime.now(UTC)


def parse_uri(xml_text: str) -> str | None:
    """Return the ``Uri`` from a GetSnapshotUri/GetStreamUri response, or None."""
    root = soap.parse_xml(xml_text)
    if root is None:
        return None
    return soap.find_text(root, ".//tt:Uri")


def _resolve_watchdog_seconds() -> float:
    """Return the configured idle watchdog for a PullPoint pull cycle, in seconds.

    Falls back to the model default when no runtime context is installed, so the
    watchdog is always armed rather than silently disabled. In a running service
    the context is always present; the fallback only matters for the event source
    exercised outside a fully configured application context.
    """
    from ..runtime import get_context

    try:
        return get_context().settings.capture.onvif_watchdog_seconds
    except (RuntimeError, AttributeError):
        from ..config.settings import CaptureSettings

        default = CaptureSettings.model_fields["onvif_watchdog_seconds"].default
        assert isinstance(default, float)
        return default


class OnvifAdapter(CameraAdapter):
    """Capture from an ONVIF camera by resolving its media-service URIs."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        address: str,
        credentials: tuple[str, str] | None = None,
        snapshot_uri: str | None = None,
        stream_uri: str | None = None,
        timeout: float = 10.0,
        ffmpeg_binary: str = "ffmpeg",
        stream_id: str | None = None,
        uri_cache: AdapterCache | None = None,
        auth_state: CameraAuthState | None = None,
        allow_rtsp_fallback: bool = True,
        contain_default: bool = False,
    ) -> None:
        self._client = client
        # True when ``credentials`` are the bound global default: the RTSP
        # fallback then dials only a validated address inside the allowed
        # private subnets (#268).
        self._contain_default = contain_default
        # False for a certificate-pinned camera: the RTSP grab is not covered by
        # the pin and would carry the camera's credentials outside it (#228).
        self._allow_rtsp_fallback = allow_rtsp_fallback
        # Never keep a ``user:pass@`` from a legacy address: httpx would turn it
        # into a preemptive Basic header on every request (#69 CAM-S2).
        self._address = split_userinfo(address)[0]
        self._credentials = credentials
        # The camera session's HTTP auth negotiation state for the snapshot GET
        # (#69 CAM-S1, CAM-P1); SOAP calls authenticate with WS-Security in the
        # envelope instead. A fresh one with the probe policy when none is given.
        self._auth_state = auth_state if auth_state is not None else CameraAuthState()
        # Pre-configured URIs (operator-provided) short-circuit SOAP resolution.
        self._snapshot_uri = snapshot_uri
        self._stream_uri = stream_uri
        # Values resolved over SOAP (profile token, media URIs) are kept in the
        # camera session's cache across captures when one is given (#69
        # CAM-P1), so a capture costs one snapshot GET instead of three
        # requests. Operator-provided URIs never go through it.
        self._uri_cache = uri_cache
        # The selected media-profile token, or None to use the device's first
        # profile. A non-None stream_id seeds the profile token so the snapshot/
        # stream URI resolvers target that profile. (When ``snapshot_uri`` /
        # ``stream_uri`` are operator-provided they short-circuit resolution, so
        # a stream_id is ignored on those pre-configured paths.)
        self._profile_token: str | None = stream_id
        self._timeout = timeout
        # Used only on the RTSP fallback path when the device exposes no snapshot
        # endpoint; passed through so the fallback grab uses the same ffmpeg as
        # the rest of the application (bundled when frozen).
        self._ffmpeg_binary = ffmpeg_binary
        # The camera's clock offset when there is no session cache to keep it in
        # (an ad-hoc probe), as ``(offset_seconds, measured_at_monotonic)``.
        self._local_clock_offset: tuple[float, float] | None = None
        # Called (on the event loop) once an event subscription is established,
        # so the supervisor can tell "subscribed and quiet" from "never subscribed"
        # (#69 CAM-R7). Set by the event-listener factory; never required.
        self.on_subscribed: Callable[[], Awaitable[None]] | None = None

    async def _guard_soap_url(self, url: str) -> None:
        """Validate a device/media SOAP target against the camera deny-list.

        The single chokepoint :meth:`_soap_call` funnels every device- and
        media-service request through (GetProfiles, GetSnapshotUri, GetStreamUri,
        GetServices, GetGeoLocation, GetHostname). The target URL is derived from
        the operator-supplied camera address, so a misconfigured -- or hostile --
        address could otherwise point an internal POST at loopback, link-local, or
        the cloud-metadata endpoint. Routing it through the SSRF guard here closes
        that hole at the one point every such call passes through, fail-closed.

        Uses the camera/scan policy (admin private opt-in honoured; loopback/
        link-local/metadata never relaxed) -- the same policy the snapshot, stream,
        and event paths use -- so a legitimately-configured private camera still
        works. A denied target surfaces as :class:`UnreachableCaptureError` so it
        flows through the adapter's existing capture-failure handling.

        The lookup runs off the event loop and an allowed answer is cached, so
        the client's pinned connection dials the address validated here (see
        :mod:`.clients`).
        """
        try:
            await check_camera_url_async(url)
        except SsrfError as exc:
            raise UnreachableCaptureError(f"device URL blocked: {exc}") from exc

    async def _require_own_host(self, uri: str, kind: str) -> None:
        """Refuse a device-supplied URI that does not name this camera's host.

        A device answers ``GetSnapshotUri``, ``GetStreamUri``, ``GetServices``
        and ``CreatePullPointSubscription`` with URIs of its own choosing, and
        the camera's credentials go to whatever host they name (a Digest
        answer, WS-Security in a SOAP envelope, ffmpeg's URL). A rogue or
        compromised device could otherwise name a machine that challenges and
        collects them (#69 CAM-S5, #193). So a URI must name the configured
        host: the same name or IP, or an IP literal that the configured name
        resolves to (a camera added by name that reports its IP). Only hosts are
        compared -- the stream is ``rtsp://`` on another port by design.

        The URI is not fetched here, and nothing is sent to a different host.
        An operator-stored ``snapshot_uri`` / ``stream_uri`` is not a device
        answer and is never checked: saving one on the camera form is the
        explicit acceptance of its host, and re-typing the password after a
        host change is what lets stored credentials follow it
        (:func:`~timelapse_manager.security.credential_binding.introduces_new_host`).
        The host check rejects userinfo, whitespace, backslashes and control
        characters first, so the parser consuming the URI cannot read a
        different host than this check did.

        :param kind: ``"snapshot"``, ``"stream"``, ``"events"`` or
            ``"subscription"``, named in the error.
        :raises UnreachableCaptureError: the URI is malformed or names another
            host. Not an :class:`OtherCaptureError`, so a refused snapshot URI
            never falls back to RTSP.
        """
        uri_host = target_host(uri, allow_userinfo=False)
        if uri_host is None:
            raise UnreachableCaptureError(f"camera returned a malformed {kind} URI")
        own_host = target_host(self._address)
        if own_host is not None and uri_host == own_host:
            return
        if own_host is not None and await self._hosts_share_address(own_host, uri_host):
            return
        raise UnreachableCaptureError(
            f"camera returned a {kind} URI for another host ({uri_host}); "
            "enter that URI on the camera form to accept it"
        )

    @staticmethod
    async def _hosts_share_address(own_host: str, uri_host: str) -> bool:
        """Whether ``uri_host`` is an IP literal that the camera's own host has.

        Only a *literal* is an alias: a camera added by name that reports its
        own IP. A different **name** is refused even when it resolves to the
        camera's address right now, because the capture client resolves the
        name again when it connects (it is not pinned), so a device could answer
        the check with the camera's IP and the connection with its own.
        """
        try:
            literal = ipaddress.ip_address(uri_host)
        except ValueError:
            return False
        from ..runtime import get_context

        ssrf = get_context().settings.ssrf
        try:
            own_ips = await pinned_addresses_async(
                own_host,
                allow_private=True,
                allowed_private_subnets=ssrf.allowed_private_subnets,
            )
        except SsrfError:
            return False
        return str(literal) in own_ips

    async def _soap_call(
        self, url: str, body: str, *, authenticate: bool = True
    ) -> str:
        """POST a SOAP envelope and return the response text.

        The target is validated through the SSRF guard before the POST: the URL
        is built from the operator-supplied camera address, so guarding here -- the
        shared device/media chokepoint -- keeps an internal POST from being aimed
        at a denied host.

        The response body is streamed and capped at
        :data:`._bounded.SOAP_RESPONSE_MAX_BYTES` (#69 CAM-S3).

        The WS-Security token carries the camera's own time (see
        :meth:`_clock_offset`). ``authenticate=False`` sends no token at all
        (GetSystemDateAndTime, which is how that offset is measured).

        :raises AuthCaptureError: the device refused the login (a 401/403, or
            an authentication SOAP fault; #69 CAM-R9).
        :raises CaptureError: mapped from a denied target (SSRF), from
            transport/timeout/status failures, or from an oversized body.
        """
        await self._guard_soap_url(url)
        credentials = self._credentials if authenticate else None
        offset = await self._clock_offset() if credentials is not None else None
        payload = soap.envelope(body, credentials, clock_offset=offset or timedelta(0))
        headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
        try:
            response = await read_capped_response(
                self._client,
                "POST",
                url,
                max_bytes=SOAP_RESPONSE_MAX_BYTES,
                content=payload,
                headers=headers,
                timeout=request_timeout(self._client, self._timeout),
                read_error_body=True,
            )
        except httpx.PoolTimeout as exc:
            # Before TimeoutException (its base): no free local connection is
            # not a camera failure (#69 CAM-P2).
            raise pool_exhausted_error(url) from exc
        except httpx.TimeoutException as exc:
            raise TimeoutCaptureError(f"onvif call timed out: {url}") from exc
        except httpx.TransportError as exc:
            raise UnreachableCaptureError(
                f"cannot reach onvif device {url}: {exc}"
            ) from exc
        self._check_soap_status(
            response.status_code, response.text, url, "onvif call", authenticate
        )
        return response.text

    def _check_soap_status(
        self, status_code: int, text: str, url: str, what: str, authenticated: bool
    ) -> None:
        """Raise for a failed SOAP reply; note a login failure for the clock check.

        A refused login may mean the camera's clock drifted since it was
        measured, so an authentication failure marks the cached offset for
        re-measurement (see :meth:`_note_auth_failure`).
        """
        try:
            _raise_for_soap_status(status_code, text, url, what)
        except AuthCaptureError:
            if authenticated:
                self._note_auth_failure()
            raise

    # -- Camera clock -------------------------------------------------------

    async def _clock_offset(self) -> timedelta:
        """Return how far the camera's clock is ahead of ours, measured once.

        The offset is read with one unauthenticated GetSystemDateAndTime and kept
        in the camera session's cache, so a listener and its captures share it. A
        device that will not say (an error, or a login required) gets no offset,
        which is the previous behaviour (#69 CAM-R9).
        """
        stored = self._stored_clock_offset()
        if stored is None:
            stored = await self._measure_clock_offset()
        return timedelta(seconds=stored[0])

    def _stored_clock_offset(self) -> tuple[float, float] | None:
        """Return the cached ``(seconds, measured_at)`` offset, or ``None``."""
        if self._local_clock_offset is not None:
            return self._local_clock_offset
        if self._uri_cache is None:
            return None
        value = self._uri_cache.get(_CLOCK_OFFSET_KEY)
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and all(isinstance(part, float) for part in value)
        ):
            return (value[0], value[1])
        return None

    def _store_clock_offset(self, seconds: float) -> tuple[float, float]:
        """Remember a measured offset where the next call (and capture) finds it."""
        entry = (seconds, time.monotonic())
        if self._uri_cache is not None:
            self._uri_cache.put(_CLOCK_OFFSET_KEY, entry)
        else:
            self._local_clock_offset = entry
        return entry

    async def _measure_clock_offset(self) -> tuple[float, float]:
        """Ask the camera for its UTC time and store the offset from ours."""
        url = _device_url(self._address)
        started = datetime.now(UTC)
        try:
            text = await self._soap_call(
                url, soap.get_system_date_and_time_body(), authenticate=False
            )
        except CaptureError as exc:
            logger.debug("onvif clock offset unavailable: %s", exc)
            return self._store_clock_offset(0.0)
        finished = datetime.now(UTC)
        camera_time = soap.parse_utc_datetime(text)
        if camera_time is None:
            return self._store_clock_offset(0.0)
        midpoint = started + (finished - started) / 2
        seconds = round((camera_time - midpoint).total_seconds())
        if abs(seconds) <= _CLOCK_OFFSET_NOISE_SECONDS:
            seconds = 0
        elif abs(seconds) > _CLOCK_OFFSET_MAX_SECONDS:
            # A clock a day or more out is a broken or hostile answer, not skew
            # to correct for (and a huge shift could overflow a timestamp).
            logger.debug("ignoring implausible onvif clock offset of %ds", seconds)
            seconds = 0
        return self._store_clock_offset(float(seconds))

    def _note_auth_failure(self) -> None:
        """Drop an old clock offset after a refused login, so it is re-measured."""
        stored = self._stored_clock_offset()
        if stored is None:
            return
        if time.monotonic() - stored[1] < _CLOCK_OFFSET_REFRESH_SECONDS:
            return
        self._local_clock_offset = None
        if self._uri_cache is not None:
            self._uri_cache.drop(_CLOCK_OFFSET_KEY)

    def _cached(self, key: tuple[str, str | None]) -> str | None:
        """Return a value from the shared URI cache, if any."""
        if self._uri_cache is None:
            return None
        value = self._uri_cache.get(key)
        return value if isinstance(value, str) else None

    def _remember(self, key: tuple[str, str | None], value: str) -> None:
        """Store a resolved value in the shared URI cache, if any."""
        if self._uri_cache is not None:
            self._uri_cache.put(key, value)

    def forget_resolved_uris(self) -> None:
        """Drop the shared URI cache, so the next capture resolves afresh."""
        if self._uri_cache is not None:
            self._uri_cache.clear()

    async def _resolve_profile_token(self) -> str:
        if self._profile_token is not None:
            return self._profile_token
        cached = self._cached(("profile", None))
        if cached is not None:
            self._profile_token = cached
            return cached
        url = _device_url(self._address)
        text = await self._soap_call(url, _GET_PROFILES_BODY)
        token = parse_first_profile_token(text)
        if token is None:
            raise OtherCaptureError("onvif device returned no media profiles")
        self._profile_token = token
        self._remember(("profile", None), token)
        return token

    async def _resolve_snapshot_uri(self) -> str:
        if self._snapshot_uri is not None:
            return self._snapshot_uri
        token = await self._resolve_profile_token()
        cached = self._cached(("snapshot", token))
        if cached is not None:
            self._snapshot_uri = cached
            return cached
        text = await self._soap_call(
            _device_url(self._address), get_snapshot_uri_body(token)
        )
        uri = parse_uri(text)
        if uri is None:
            raise OtherCaptureError("onvif device returned no snapshot URI")
        await self._require_own_host(uri, "snapshot")
        self._snapshot_uri = uri
        self._remember(("snapshot", token), uri)
        return uri

    async def _resolve_stream_uri(self) -> str:
        if self._stream_uri is not None:
            return self._stream_uri
        token = await self._resolve_profile_token()
        cached = self._cached(("stream", token))
        if cached is not None:
            # Validated before it was cached; RtspAdapter re-checks the host on
            # every grab, which closes the DNS-rebinding window on reuse.
            self._stream_uri = cached
            return cached
        text = await self._soap_call(
            _device_url(self._address), get_stream_uri_body(token)
        )
        uri = parse_uri(text)
        if uri is None:
            raise OtherCaptureError("onvif device returned no stream URI")
        await self._require_own_host(uri, "stream")
        # The stream URI comes from the device's SOAP response and is therefore
        # attacker-influenceable: a rogue/compromised camera can point it at
        # loopback, the cloud-metadata endpoint, or an internal host. Validate its
        # host through the SSRF guard *before* caching, so a poisoned URI is
        # rejected at this boundary and never stored. (The per-capture re-check in
        # RtspAdapter.capture closes the separate DNS-rebinding window on reuse.)
        await _guard_stream_uri(uri)
        self._stream_uri = uri
        self._remember(("stream", token), uri)
        return uri

    async def resolve_uris(self) -> tuple[str | None, str | None]:
        """Resolve the snapshot and stream URIs without capturing a frame.

        Best-effort, used to enrich a discovered device with its media URIs:
        each URI is resolved independently through the same SOAP resolvers the
        capture path uses, so the configured credentials and the stream-URI SSRF
        guard both apply. A failure on one branch yields ``None`` for that URI
        rather than sinking the other -- the snapshot can succeed while the
        stream fails, or vice versa.

        Only :class:`CaptureError` (the family the capture path treats as
        recoverable) is caught: transport/timeout errors are already mapped to it
        in :meth:`_soap_call`, missing-URI/parse failures surface as
        :class:`OtherCaptureError`, and a stream URI that fails the SSRF guard
        surfaces as :class:`UnreachableCaptureError`. Anything outside that family
        is a genuine bug and is left to propagate.

        :returns: ``(snapshot_uri, stream_uri)``; either element is ``None`` when
            that URI could not be resolved. Neither value carries credentials.
        """
        snapshot_uri: str | None = None
        stream_uri: str | None = None
        try:
            snapshot_uri = await self._resolve_snapshot_uri()
        except CaptureError as exc:
            logger.debug(
                "onvif snapshot resolution failed for %s: %s", self._address, exc
            )
        try:
            stream_uri = await self._resolve_stream_uri()
        except CaptureError as exc:
            logger.debug(
                "onvif stream resolution failed for %s: %s", self._address, exc
            )
        return snapshot_uri, stream_uri

    @property
    def capture_budget_seconds(self) -> float:
        """The RTSP fallback's budget, so the shared capture timeout cannot cut it."""
        return rtsp_capture_budget_seconds()

    async def capture(self) -> CapturedFrame:
        # Prefer the snapshot URI (cheaper, no transcoding); fall back to a
        # single-frame RTSP grab if the device exposes no snapshot endpoint.
        try:
            return await self._capture_once()
        except CaptureError:
            # A failure through a cached URI (a 401/404 after the camera was
            # reconfigured, say) must not pin a stale value for an hour: the
            # next capture resolves everything afresh.
            self.forget_resolved_uris()
            raise

    async def _capture_once(self) -> CapturedFrame:
        # Only a device that answered but has no snapshot endpoint
        # (OtherCaptureError) falls back to RTSP. A refused login
        # (AuthCaptureError) propagates: the same credentials would fail over
        # RTSP too, and an auth failure must reach camera health as one
        # (#69 CAM-R9).
        try:
            snapshot_uri = await self._resolve_snapshot_uri()
        except OtherCaptureError:
            if not self._allow_rtsp_fallback:
                raise
            stream_uri = await self._resolve_stream_uri()
            rtsp = RtspAdapter(
                stream_uri,
                self._credentials,
                ffmpeg_binary=self._ffmpeg_binary,
                contain_default=self._contain_default,
            )
            return await rtsp.capture()
        image_bytes = await http_get_image(
            self._client,
            snapshot_uri,
            self._credentials,
            self._timeout,
            auth_state=self._auth_state,
        )
        return frame_from_bytes(image_bytes)

    async def validate_connection(self) -> ValidationResult:
        try:
            await self.capture()
        except CaptureError as exc:
            return ValidationResult(ok=False, reason=exc.reason, message=exc.message)
        return ValidationResult(
            ok=True, reason=None, message="resolved profile and captured a frame"
        )

    async def get_geolocation(self) -> GeoLocation | None:
        # ONVIF exposes geolocation via the device GetGeoLocation operation,
        # which is optional and inconsistently implemented; treat its absence as
        # simply "no location" rather than an error.
        body = "<tds:GetGeoLocation/>"
        try:
            text = await self._soap_call(_device_url(self._address), body)
        except CaptureError:
            return None
        root = soap.parse_xml(text)
        if root is None:
            return None
        location = root.find(".//tt:Location", soap.NS)
        if location is None:
            return None
        lat = location.get("lat")
        lon = location.get("lon")
        if lat is None or lon is None:
            return None
        try:
            latitude = float(lat)
            longitude = float(lon)
        except ValueError:
            return None
        # The device is untrusted: ``float()`` accepts ``nan``/``inf``, and a
        # device can report any magnitude. Treat an impossible value as "no
        # location" rather than letting it reach the solar-anchor maths.
        if not valid_coordinates(latitude, longitude):
            return None
        return GeoLocation(latitude=latitude, longitude=longitude, source="camera")

    async def get_device_hostname(self) -> str | None:
        # ONVIF exposes the device hostname via the device GetHostname operation,
        # whose response carries a tds:HostnameInformation with a tt:Name holding
        # the configured name. Like geolocation it is best-effort: an unreachable
        # device, a device that does not implement it, or an empty/absent name all
        # read as "no hostname" (None) rather than an error.
        body = "<tds:GetHostname/>"
        try:
            text = await self._soap_call(_device_url(self._address), body)
        except CaptureError:
            return None
        root = soap.parse_xml(text)
        if root is None:
            return None
        # The hostname lives in tt:Name under the HostnameInformation element.
        # Search by local name so a device that emits it in the tds: namespace
        # (some firmware does) is matched too.
        name = soap.find_text(root, ".//tt:Name")
        if name is None:
            name = _find_text_local(root, "Name")
        return name

    async def capabilities(self) -> CameraCapabilities:
        # Resolution enumeration would require parsing each profile's video
        # encoder configuration; not implemented in the lean path.
        return CameraCapabilities(supported_resolutions=[])

    async def list_stream_profiles(self) -> StreamProfileResult:
        # Query the Media service for the device's profiles. _soap_call maps
        # transport/timeout/status failures to CaptureError, which we report as a
        # clean ok=False so a caller never crashes; the profile token is the id a
        # later capture targets.
        try:
            text = await self._soap_call(_device_url(self._address), _GET_PROFILES_BODY)
        except CaptureError as exc:
            return StreamProfileResult(profiles=[], ok=False, message=exc.message)
        profiles = parse_profiles(text)
        if not profiles:
            return StreamProfileResult(
                profiles=[],
                ok=False,
                message="onvif device returned no media profiles",
            )
        return StreamProfileResult(profiles=profiles, ok=True)

    # -- Events -------------------------------------------------------------

    def _events_fallback_url(self) -> str:
        """Return the conventional events services URL for this address."""
        if self._address.startswith(("http://", "https://")):
            base = self._address.rstrip("/")
            return f"{base}{EVENTS_SERVICE_PATH}"
        return f"http://{self._address}{EVENTS_SERVICE_PATH}"

    async def _guarded_event_soap_call(
        self,
        url: str,
        body: str,
        *,
        action: str,
        reference_parameters: str = "",
        timeout: float | None = None,
    ) -> str:
        """POST an event SOAP envelope through the camera SSRF guard, fail-closed.

        Unlike the media/device :meth:`_soap_call`, every event call is guarded
        immediately before sending: the PullPoint subscription address is
        camera-returned and re-used over hours, so its host is re-validated on
        each request to close the DNS-rebinding window. The WS-Addressing headers
        (and, when present, the echoed subscription reference parameter) are added
        here so each call carries the action/target the firmware requires. The
        response body is streamed and capped at
        :data:`._bounded.SOAP_RESPONSE_MAX_BYTES` (#69 CAM-S3).

        :raises AuthCaptureError: the device refused the login (a 401/403, or
            an authentication SOAP fault; #69 CAM-R9).
        :raises CaptureError: mapped from a denied target (SSRF), transport,
            timeout, non-2xx status or an oversized body -- the family the
            listener treats as recoverable (backoff + re-subscribe).
        """
        await self._guard_event_url(url)
        headers_xml = soap.addressing_headers(
            action, url, reference_parameters=reference_parameters
        )
        offset = await self._clock_offset() if self._credentials is not None else None
        payload = soap.envelope(
            body,
            self._credentials,
            headers_xml,
            clock_offset=offset or timedelta(0),
        )
        headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
        try:
            response = await read_capped_response(
                self._client,
                "POST",
                url,
                max_bytes=SOAP_RESPONSE_MAX_BYTES,
                content=payload,
                headers=headers,
                timeout=request_timeout(
                    self._client, timeout if timeout is not None else self._timeout
                ),
                read_error_body=True,
            )
        except httpx.PoolTimeout as exc:
            raise pool_exhausted_error(url) from exc
        except httpx.TimeoutException as exc:
            raise TimeoutCaptureError(f"onvif event call timed out: {url}") from exc
        except httpx.TransportError as exc:
            raise UnreachableCaptureError(
                f"cannot reach onvif device {url}: {exc}"
            ) from exc
        self._check_soap_status(
            response.status_code, response.text, url, "onvif event call", True
        )
        return response.text

    async def _guard_event_url(self, url: str) -> None:
        """Validate an event SOAP target against the camera deny-list.

        Uses the camera/scan policy (admin private opt-in honoured; loopback/
        link-local/metadata never relaxed). A denied target surfaces as
        :class:`UnreachableCaptureError` so it flows through the listener's
        recoverable-failure handling. Runs on every pull, so the lookup is off
        the event loop and cached for a short time.
        """
        try:
            await check_camera_url_async(url)
        except SsrfError as exc:
            raise UnreachableCaptureError(f"event URL blocked: {exc}") from exc

    async def _resolve_events_url(self) -> str:
        """Resolve the Events service XAddr via GetServices, else fall back.

        The device's GetServices response lists each service with its namespace
        and XAddr; the events service is the one whose namespace is the ONVIF
        events WSDL. A reachability/parse failure degrades to the conventional
        ``/onvif/services`` URL rather than failing, since on this firmware that
        is exactly what GetServices returns anyway.
        """
        try:
            text = await self._soap_call(_device_url(self._address), _GET_SERVICES_BODY)
        except CaptureError:
            return self._events_fallback_url()
        url = _parse_events_xaddr(text)
        if url is None:
            return self._events_fallback_url()
        try:
            await self._require_own_host(url, "events")
        except UnreachableCaptureError as exc:
            # The events XAddr is device-supplied and receives the login in the
            # SOAP envelope: a host other than the camera's is ignored in favour
            # of the conventional URL on the configured address (#69 CAM-S5).
            logger.warning("ignoring the device's events address: %s", exc)
            return self._events_fallback_url()
        return url

    async def list_event_topics(self) -> list[EventDescriptor]:
        # Resolve the events endpoint, query its topic catalogue, and turn each
        # raw topic path into a canonical descriptor. Every failure mode degrades
        # to an empty list (never raises) so a caller can enumerate inline.
        try:
            events_url = await self._resolve_events_url()
            text = await self._guarded_event_soap_call(
                events_url,
                soap.get_event_properties_body(),
                action=soap.ACTION_GET_EVENT_PROPERTIES,
            )
        except CaptureError as exc:
            logger.debug("onvif event-topic enumeration failed: %s", exc)
            return []
        descriptors: list[EventDescriptor] = []
        seen: set[str] = set()
        for raw_topic in soap.parse_event_topics(text):
            topic_id = eventutil.canonicalize_topic(raw_topic)
            if not topic_id or topic_id in seen:
                continue
            seen.add(topic_id)
            descriptors.append(_descriptor_for(topic_id, raw_topic))
        return descriptors

    def open_event_source(self) -> AsyncIterator[CameraEvent]:
        # The async generator defers all I/O (resolve endpoint, subscribe) to its
        # first iteration, so constructing the source is free and a backoff retry
        # gets a fresh subscription. Teardown (Unsubscribe) runs from the
        # generator's finally on consumer cancellation.
        return self._pullpoint_events()

    async def _pullpoint_events(self) -> AsyncIterator[CameraEvent]:
        """Yield canonical events from a PullPoint subscription until cancelled.

        Subscribe (CreatePullPointSubscription) on first iteration, then loop
        PullMessages (long-poll), yielding one :class:`CameraEvent` per
        notification and renewing before the termination time. On teardown the
        finally best-effort Unsubscribes (shielded, bounded) without swallowing
        the cancellation.
        """
        events_url = await self._resolve_events_url()
        create_text = await self._guarded_event_soap_call(
            events_url,
            soap.create_pullpoint_body(_SUBSCRIPTION_TERMINATION),
            action=soap.ACTION_CREATE_PULLPOINT,
        )
        reference = soap.parse_subscription_reference(create_text)
        if reference is None:
            raise OtherCaptureError("onvif device returned no subscription reference")
        sub_url, ref_params = reference
        await self._require_own_host(sub_url, "subscription")
        if self.on_subscribed is not None:
            await self.on_subscribed()
        loop = asyncio.get_running_loop()
        next_renew = loop.time() + (
            _SUBSCRIPTION_TERMINATION_SECONDS - _RENEW_MARGIN_SECONDS
        )
        watchdog_seconds = _resolve_watchdog_seconds()
        try:
            while True:
                cycle_started = loop.time()
                # Bound the renew+pull cycle with an idle watchdog. httpx's own
                # read timeout (_PULL_HTTP_TIMEOUT) covers a silent socket, but a
                # half-open camera or a slow-loris trickle that keeps resetting the
                # per-read timer can stall a cycle indefinitely -- hanging the
                # listener task so it never reaches the re-subscribe path. On
                # expiry we raise out of the generator; the supervisor's listener
                # catches it like any other source failure and re-subscribes after
                # backoff (the existing recovery path, not a new one). The yields
                # stay outside the timeout so a slow consumer is never mistaken for
                # a wedged camera.
                try:
                    async with asyncio.timeout(watchdog_seconds):
                        if loop.time() >= next_renew:
                            await self._guarded_event_soap_call(
                                sub_url,
                                soap.renew_body(_SUBSCRIPTION_TERMINATION),
                                action=soap.ACTION_RENEW,
                                reference_parameters=ref_params,
                            )
                            next_renew = loop.time() + (
                                _SUBSCRIPTION_TERMINATION_SECONDS
                                - _RENEW_MARGIN_SECONDS
                            )
                        pull_text = await self._guarded_event_soap_call(
                            sub_url,
                            soap.pull_messages_body(_PULL_TIMEOUT),
                            action=soap.ACTION_PULL_MESSAGES,
                            reference_parameters=ref_params,
                            timeout=_PULL_HTTP_TIMEOUT,
                        )
                        notifications = soap.parse_pull_messages(pull_text)
                except TimeoutError as exc:
                    raise TimeoutCaptureError(
                        f"onvif pull cycle did not complete within "
                        f"{watchdog_seconds:.0f}s; treating subscription as dead"
                    ) from exc
                for notification in notifications:
                    event = _event_from_notification(notification)
                    if event is not None:
                        yield event
                # A camera that answers an empty pull at once is not polled in a
                # tight loop: the cycle takes at least the minimum, whatever the
                # consumer spent on the events above (#69 CAM-R9).
                remainder = _MIN_PULL_CYCLE_SECONDS - (loop.time() - cycle_started)
                if remainder > 0:
                    await asyncio.sleep(remainder)
        finally:
            await self._unsubscribe(sub_url, ref_params)

    async def _unsubscribe(self, sub_url: str, ref_params: str) -> None:
        """Best-effort Unsubscribe, shielded and bounded, never raising.

        Runs in the generator's finally, which may be reached because the
        consumer cancelled us. The actual SOAP call is run as a shielded inner
        task so a cancellation propagating through this finally does not abort the
        in-flight Unsubscribe before it can release the camera's subscription; it
        is bounded by its own timeout so it cannot stall teardown. Ordinary
        failures (an unreachable camera, a timeout) are swallowed -- the
        subscription expires on its own termination time -- but ``CancelledError``
        is re-raised so the teardown that triggered this finally still propagates,
        after waiting (also bounded) for the Unsubscribe to finish.
        """
        task = asyncio.ensure_future(self._bounded_unsubscribe(sub_url, ref_params))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # We are being torn down. Let the shielded Unsubscribe run to
            # completion in the background (best-effort) and re-raise so the
            # cancellation that reached this finally still propagates.
            with contextlib.suppress(Exception):
                async with asyncio.timeout(_UNSUBSCRIBE_TIMEOUT):
                    await task
            raise
        except (CaptureError, TimeoutError) as exc:
            logger.debug("onvif unsubscribe failed (subscription will expire): %s", exc)

    async def _bounded_unsubscribe(self, sub_url: str, ref_params: str) -> None:
        """Send the Unsubscribe under the teardown budget (``asyncio.timeout``)."""
        async with asyncio.timeout(_UNSUBSCRIBE_TIMEOUT):
            await self._guarded_event_soap_call(
                sub_url,
                soap.unsubscribe_body(),
                action=soap.ACTION_UNSUBSCRIBE,
                reference_parameters=ref_params,
                timeout=_UNSUBSCRIBE_TIMEOUT,
            )

    async def close(self) -> None:
        # The HTTP client is owned by the caller; nothing to release here.
        return None
