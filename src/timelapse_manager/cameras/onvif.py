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
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

import httpx

from ..security.ssrf import SsrfError, assert_allowed_url
from . import _onvif_soap as soap
from . import events as eventutil
from .base import (
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
)
from .http_jpeg import frame_from_bytes, http_get_image
from .rtsp import RtspAdapter
from .rtsp import _guard_stream_url as _guard_stream_uri

logger = logging.getLogger(__name__)

DEVICE_SERVICE_PATH = "/onvif/device_service"
# The shared services endpoint the event operations target on this firmware. The
# real address is discovered from GetServices (the Events service XAddr); this is
# the conventional fallback when discovery is unavailable.
EVENTS_SERVICE_PATH = "/onvif/services"

_GET_PROFILES_BODY = "<trt:GetProfiles/>"
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


def _device_url(address: str) -> str:
    """Return the ONVIF device service URL for an address."""
    if address.startswith(("http://", "https://")):
        base = address.rstrip("/")
        if base.endswith(DEVICE_SERVICE_PATH):
            return base
        return f"{base}{DEVICE_SERVICE_PATH}"
    return f"http://{address}{DEVICE_SERVICE_PATH}"


def get_snapshot_uri_body(profile_token: str) -> str:
    """SOAP body requesting the snapshot URI for a media profile."""
    return (
        "<trt:GetSnapshotUri>"
        f"<trt:ProfileToken>{profile_token}</trt:ProfileToken>"
        "</trt:GetSnapshotUri>"
    )


def get_stream_uri_body(profile_token: str) -> str:
    """SOAP body requesting the RTSP stream URI for a media profile."""
    return (
        "<trt:GetStreamUri>"
        "<trt:StreamSetup>"
        "<tt:Stream xmlns:tt='http://www.onvif.org/ver10/schema'>"
        "RTP-Unicast</tt:Stream>"
        "<tt:Transport xmlns:tt='http://www.onvif.org/ver10/schema'>"
        "<tt:Protocol>RTSP</tt:Protocol></tt:Transport>"
        "</trt:StreamSetup>"
        f"<trt:ProfileToken>{profile_token}</trt:ProfileToken>"
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
    ) -> None:
        self._client = client
        self._address = address
        self._credentials = credentials
        # Pre-configured URIs (operator-provided) short-circuit SOAP resolution.
        self._snapshot_uri = snapshot_uri
        self._stream_uri = stream_uri
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

    async def _soap_call(self, url: str, body: str) -> str:
        """POST a SOAP envelope and return the response text.

        :raises CaptureError: mapped from transport/timeout/status failures.
        """
        payload = soap.envelope(body, self._credentials)
        headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
        try:
            response = await self._client.post(
                url, content=payload, headers=headers, timeout=self._timeout
            )
        except httpx.TimeoutException as exc:
            raise TimeoutCaptureError(f"onvif call timed out: {url}") from exc
        except httpx.TransportError as exc:
            raise UnreachableCaptureError(
                f"cannot reach onvif device {url}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise OtherCaptureError(
                f"onvif call to {url} returned {response.status_code}"
            )
        return response.text

    async def _resolve_profile_token(self) -> str:
        if self._profile_token is not None:
            return self._profile_token
        url = _device_url(self._address)
        text = await self._soap_call(url, _GET_PROFILES_BODY)
        token = parse_first_profile_token(text)
        if token is None:
            raise OtherCaptureError("onvif device returned no media profiles")
        self._profile_token = token
        return token

    async def _resolve_snapshot_uri(self) -> str:
        if self._snapshot_uri is not None:
            return self._snapshot_uri
        token = await self._resolve_profile_token()
        text = await self._soap_call(
            _device_url(self._address), get_snapshot_uri_body(token)
        )
        uri = parse_uri(text)
        if uri is None:
            raise OtherCaptureError("onvif device returned no snapshot URI")
        self._snapshot_uri = uri
        return uri

    async def _resolve_stream_uri(self) -> str:
        if self._stream_uri is not None:
            return self._stream_uri
        token = await self._resolve_profile_token()
        text = await self._soap_call(
            _device_url(self._address), get_stream_uri_body(token)
        )
        uri = parse_uri(text)
        if uri is None:
            raise OtherCaptureError("onvif device returned no stream URI")
        # The stream URI comes from the device's SOAP response and is therefore
        # attacker-influenceable: a rogue/compromised camera can point it at
        # loopback, the cloud-metadata endpoint, or an internal host. Validate its
        # host through the SSRF guard *before* caching, so a poisoned URI is
        # rejected at this boundary and never stored. (The per-capture re-check in
        # RtspAdapter.capture closes the separate DNS-rebinding window on reuse.)
        await _guard_stream_uri(uri)
        self._stream_uri = uri
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

    async def capture(self) -> CapturedFrame:
        # Prefer the snapshot URI (cheaper, no transcoding); fall back to a
        # single-frame RTSP grab if the device exposes no snapshot endpoint.
        try:
            snapshot_uri = await self._resolve_snapshot_uri()
        except OtherCaptureError:
            stream_uri = await self._resolve_stream_uri()
            rtsp = RtspAdapter(
                stream_uri, self._credentials, ffmpeg_binary=self._ffmpeg_binary
            )
            return await rtsp.capture()
        image_bytes = await http_get_image(
            self._client, snapshot_uri, self._credentials, self._timeout
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
            return GeoLocation(
                latitude=float(lat), longitude=float(lon), source="camera"
            )
        except ValueError:
            return None

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
        here so each call carries the action/target the firmware requires.

        :raises CaptureError: mapped from a denied target (SSRF), transport,
            timeout, or non-2xx status -- the family the listener treats as
            recoverable (backoff + re-subscribe).
        """
        self._guard_event_url(url)
        headers_xml = soap.addressing_headers(
            action, url, reference_parameters=reference_parameters
        )
        payload = soap.envelope(body, self._credentials, headers_xml)
        headers = {"Content-Type": "application/soap+xml; charset=utf-8"}
        try:
            response = await self._client.post(
                url,
                content=payload,
                headers=headers,
                timeout=timeout if timeout is not None else self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise TimeoutCaptureError(f"onvif event call timed out: {url}") from exc
        except httpx.TransportError as exc:
            raise UnreachableCaptureError(
                f"cannot reach onvif device {url}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise OtherCaptureError(
                f"onvif event call to {url} returned {response.status_code}"
            )
        return response.text

    def _guard_event_url(self, url: str) -> None:
        """Validate an event SOAP target against the camera deny-list.

        Uses the camera/scan policy (admin private opt-in honoured; loopback/
        link-local/metadata never relaxed). A denied target surfaces as
        :class:`UnreachableCaptureError` so it flows through the listener's
        recoverable-failure handling.
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
        return url or self._events_fallback_url()

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
        loop = asyncio.get_running_loop()
        next_renew = loop.time() + (
            _SUBSCRIPTION_TERMINATION_SECONDS - _RENEW_MARGIN_SECONDS
        )
        try:
            while True:
                if loop.time() >= next_renew:
                    await self._guarded_event_soap_call(
                        sub_url,
                        soap.renew_body(_SUBSCRIPTION_TERMINATION),
                        action=soap.ACTION_RENEW,
                        reference_parameters=ref_params,
                    )
                    next_renew = loop.time() + (
                        _SUBSCRIPTION_TERMINATION_SECONDS - _RENEW_MARGIN_SECONDS
                    )
                pull_text = await self._guarded_event_soap_call(
                    sub_url,
                    soap.pull_messages_body(_PULL_TIMEOUT),
                    action=soap.ACTION_PULL_MESSAGES,
                    reference_parameters=ref_params,
                    timeout=_PULL_HTTP_TIMEOUT,
                )
                for notification in soap.parse_pull_messages(pull_text):
                    event = _event_from_notification(notification)
                    if event is not None:
                        yield event
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
        is re-raised so the teardown that triggered this finally still propagates.
        """
        task = asyncio.ensure_future(
            asyncio.wait_for(
                self._guarded_event_soap_call(
                    sub_url,
                    soap.unsubscribe_body(),
                    action=soap.ACTION_UNSUBSCRIBE,
                    reference_parameters=ref_params,
                    timeout=_UNSUBSCRIBE_TIMEOUT,
                ),
                timeout=_UNSUBSCRIBE_TIMEOUT,
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # We are being torn down. Let the shielded Unsubscribe run to
            # completion in the background (best-effort) and re-raise so the
            # cancellation that reached this finally still propagates.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=_UNSUBSCRIBE_TIMEOUT)
            raise
        except (CaptureError, TimeoutError) as exc:
            logger.debug("onvif unsubscribe failed (subscription will expire): %s", exc)

    async def close(self) -> None:
        # The HTTP client is owned by the caller; nothing to release here.
        return None
