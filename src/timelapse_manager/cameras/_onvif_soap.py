"""Minimal ONVIF SOAP helpers built only on httpx and stdlib XML.

This deliberately avoids a full ONVIF/WSDL client library. The handful of ONVIF
operations the application needs (probe the device, read the media profiles,
resolve a snapshot/stream URI, read the device's geolocation) are expressed as
hand-written SOAP envelopes and parsed with :mod:`xml.etree.ElementTree`. That
keeps the dependency surface tiny and the behaviour transparent, at the cost of
covering only the operations below.

WS-Security (UsernameToken with a digested password) is included so cameras that
require authentication for these calls are supported; cameras that allow
anonymous device/media queries work without credentials too.
"""

from __future__ import annotations

import base64
import hashlib
import os
import uuid
from datetime import UTC, datetime
from xml.etree import ElementTree as ET

# XML namespaces used across ONVIF SOAP messages.
NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "wsse": (
        "http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-secext-1.0.xsd"
    ),
    "wsu": (
        "http://docs.oasis-open.org/wss/2004/01/"
        "oasis-200401-wss-wssecurity-utility-1.0.xsd"
    ),
    "tds": "http://www.onvif.org/ver10/device/wsdl",
    "trt": "http://www.onvif.org/ver10/media/wsdl",
    "tt": "http://www.onvif.org/ver10/schema",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "a": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    # Event namespaces. ``tev`` carries the ONVIF event operations
    # (CreatePullPointSubscription, PullMessages, GetEventProperties); ``wsnt`` is
    # the WS-BaseNotification surface (NotificationMessage, Renew, Unsubscribe);
    # ``wsa`` is the WS-Addressing 2005/08 dialect the event service requires on
    # every call; ``wstop`` is the topic-set schema the event-properties response
    # nests its topic tree in.
    "tev": "http://www.onvif.org/ver10/events/wsdl",
    "wsnt": "http://docs.oasis-open.org/wsn/b-2",
    "wsa": "http://www.w3.org/2005/08/addressing",
    "wstop": "http://docs.oasis-open.org/wsn/t-1",
}

# The WS-Addressing target every event call must carry when, as on this firmware,
# the subscription manager address is the bare ``/onvif/services`` URL. ``wsa:To``
# is the services URL itself; the subscription is disambiguated by the echoed
# SubscriptionId reference parameter, not by a distinct address.
WSA_ANONYMOUS_REPLY = "http://www.w3.org/2005/08/addressing/role/anonymous"

_PASSWORD_TYPE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
_NONCE_ENCODING = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-soap-message-security-1.0#Base64Binary"
)


def _security_header(username: str, password: str) -> str:
    """Build a WS-Security UsernameToken header with a digested password.

    The digest is ``Base64(SHA1(nonce + created + password))`` per the ONVIF /
    WS-Security UsernameToken profile.
    """
    nonce = os.urandom(16)
    created = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(
        hashlib.sha1(
            nonce + created.encode("utf-8") + password.encode("utf-8")
        ).digest()
    ).decode("ascii")
    nonce_b64 = base64.b64encode(nonce).decode("ascii")
    return (
        f'<wsse:Security xmlns:wsse="{NS["wsse"]}" '
        f'xmlns:wsu="{NS["wsu"]}">'
        "<wsse:UsernameToken>"
        f"<wsse:Username>{username}</wsse:Username>"
        f'<wsse:Password Type="{_PASSWORD_TYPE}">{digest}</wsse:Password>'
        f'<wsse:Nonce EncodingType="{_NONCE_ENCODING}">{nonce_b64}</wsse:Nonce>'
        f"<wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken>"
        "</wsse:Security>"
    )


def envelope(
    body: str,
    credentials: tuple[str, str] | None,
    extra_headers: str = "",
) -> str:
    """Wrap a SOAP body in an envelope, adding WS-Security if credentials given.

    ``extra_headers`` is raw SOAP-header XML appended inside ``<s:Header>``
    alongside the optional WS-Security token -- used to carry the WS-Addressing
    headers and the echoed subscription reference parameter the event calls
    require. It defaults to ``""``, so the existing media/device calls emit a
    byte-identical envelope to before (a ``<s:Header>`` is added only when there
    is at least a security token or an extra header to put in it).

    The envelope declares the event namespaces (``tev``/``wsnt``/``wsa``) up
    front so the bodies and headers below can use those prefixes without
    redeclaring them; the unused declarations are inert for the device/media
    calls.
    """
    header_content = extra_headers
    if credentials is not None:
        header_content += _security_header(*credentials)
    header = f"<s:Header>{header_content}</s:Header>" if header_content else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS["s"]}" '
        f'xmlns:tds="{NS["tds"]}" xmlns:trt="{NS["trt"]}" '
        f'xmlns:tev="{NS["tev"]}" xmlns:wsnt="{NS["wsnt"]}" '
        f'xmlns:wsa="{NS["wsa"]}" xmlns:tt="{NS["tt"]}">'
        f"{header}<s:Body>{body}</s:Body></s:Envelope>"
    )


def addressing_headers(
    action: str,
    to: str,
    *,
    reference_parameters: str = "",
) -> str:
    """Build the WS-Addressing SOAP headers an event call requires.

    Every ONVIF event operation on this firmware needs ``wsa:Action`` (the
    operation's WSDL action URI), ``wsa:To`` (the service/subscription address), a
    fresh ``wsa:MessageID`` (a ``urn:uuid:`` so each request is distinct), and an
    anonymous ``wsa:ReplyTo``. ``reference_parameters`` is the raw XML of the
    subscription's ReferenceParameters subtree (the ``dom0:SubscriptionId``
    element) echoed as a header on PullMessages/Renew/Unsubscribe so the camera
    can identify which subscription the call targets; it is empty for
    CreatePullPointSubscription and the stateless GetEventProperties.
    """
    message_id = f"urn:uuid:{uuid.uuid4()}"
    return (
        f"<wsa:Action>{action}</wsa:Action>"
        f"<wsa:MessageID>{message_id}</wsa:MessageID>"
        "<wsa:ReplyTo>"
        f"<wsa:Address>{WSA_ANONYMOUS_REPLY}</wsa:Address>"
        "</wsa:ReplyTo>"
        f"<wsa:To>{to}</wsa:To>"
        f"{reference_parameters}"
    )


def parse_xml(text: str) -> ET.Element | None:
    """Parse SOAP/XML text, returning the root element or None on bad XML."""
    try:
        return ET.fromstring(text)
    except ET.ParseError:
        return None


def find_text(element: ET.Element, path: str) -> str | None:
    """Return stripped text at a namespaced ElementPath, or None if absent."""
    found = element.find(path, NS)
    if found is None or found.text is None:
        return None
    text = found.text.strip()
    return text or None


# WS-Addressing action URIs for the event operations, captured from the live
# spike. ``wsa:Action`` must carry these verbatim or the camera faults the call.
ACTION_CREATE_PULLPOINT = (
    "http://www.onvif.org/ver10/events/wsdl/EventPortType/"
    "CreatePullPointSubscriptionRequest"
)
ACTION_PULL_MESSAGES = (
    "http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest"
)
ACTION_RENEW = "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/RenewRequest"
ACTION_UNSUBSCRIBE = (
    "http://docs.oasis-open.org/wsn/bw-2/SubscriptionManager/UnsubscribeRequest"
)
ACTION_GET_EVENT_PROPERTIES = (
    "http://www.onvif.org/ver10/events/wsdl/EventPortType/GetEventPropertiesRequest"
)


def get_event_properties_body() -> str:
    """SOAP body requesting the device's event topic catalogue."""
    return "<tev:GetEventProperties/>"


def create_pullpoint_body(initial_termination: str = "PT300S") -> str:
    """SOAP body that creates a pull-point subscription.

    ``initial_termination`` is an XSD duration after which the camera drops the
    subscription unless renewed; the loop renews well before it elapses.
    """
    return (
        "<tev:CreatePullPointSubscription>"
        f"<tev:InitialTerminationTime>{initial_termination}"
        "</tev:InitialTerminationTime>"
        "</tev:CreatePullPointSubscription>"
    )


def pull_messages_body(timeout: str = "PT15S", message_limit: int = 10) -> str:
    """SOAP body that long-polls for up to ``message_limit`` messages.

    ``timeout`` is the XSD duration the camera holds the request open waiting for
    events before returning an empty response; a short value keeps the loop
    responsive to teardown while still amortising the request cost.
    """
    return (
        "<tev:PullMessages>"
        f"<tev:Timeout>{timeout}</tev:Timeout>"
        f"<tev:MessageLimit>{message_limit}</tev:MessageLimit>"
        "</tev:PullMessages>"
    )


def renew_body(termination: str = "PT300S") -> str:
    """SOAP body that extends the subscription's termination time."""
    return (
        "<wsnt:Renew>"
        f"<wsnt:TerminationTime>{termination}</wsnt:TerminationTime>"
        "</wsnt:Renew>"
    )


def unsubscribe_body() -> str:
    """SOAP body that tears the subscription down."""
    return "<wsnt:Unsubscribe/>"


def parse_subscription_reference(xml_text: str) -> tuple[str, str] | None:
    """Parse a CreatePullPointSubscriptionResponse into (address, ref_params).

    Returns the subscription manager ``Address`` (where subsequent
    PullMessages/Renew/Unsubscribe are POSTed) and the raw XML of the
    ``ReferenceParameters`` subtree, which must be echoed verbatim as a SOAP
    header on every subsequent call so the camera can identify the subscription.
    On this firmware the address is the bare ``/onvif/services`` URL and the ref
    parameter is a ``<dom0:SubscriptionId>`` element.

    Returns ``None`` when the response carries no subscription reference (a fault
    or an unexpected shape), so the caller fails the subscription cleanly.
    """
    root = parse_xml(xml_text)
    if root is None:
        return None
    reference = root.find(".//wsnt:SubscriptionReference", NS)
    if reference is None:
        # Some stacks omit the wsnt: qualification on the reference wrapper; fall
        # back to a local-name search for the Address so a slightly different
        # response shape still resolves.
        address = _find_local_text(root, "Address")
    else:
        address = find_text(reference, "wsa:Address") or _find_local_text(
            reference, "Address"
        )
    if not address:
        return None
    ref_params = _serialize_reference_parameters(root)
    return address, ref_params


def _serialize_reference_parameters(root: ET.Element) -> str:
    """Serialise the children of a ReferenceParameters element as raw XML.

    The subtree (e.g. ``<dom0:SubscriptionId xmlns:dom0="...">3</dom0:...>``)
    must be echoed as a SOAP header on every subsequent call. Each child element
    is re-serialised with its namespace declared inline so it stands alone in the
    outbound header regardless of where the prefix was originally declared.
    """
    container = None
    for element in root.iter():
        if _local_name(element.tag) == "ReferenceParameters":
            container = element
            break
    if container is None:
        return ""
    parts: list[str] = []
    for child in container:
        parts.append(_element_to_inline_xml(child))
    return "".join(parts)


def _element_to_inline_xml(element: ET.Element) -> str:
    """Serialise one element to XML with its namespace declared inline.

    A ``{namespace}Local`` tag is rendered as ``<ns0:Local xmlns:ns0="namespace">``
    so the produced fragment is self-contained when spliced into an outbound
    header. Only the single element and its text are rendered (the reference
    parameters seen here are flat leaf elements).
    """
    tag = element.tag
    if tag.startswith("{"):
        namespace, local = tag[1:].split("}", 1)
        open_tag = f'<dom0:{local} xmlns:dom0="{namespace}">'
        close_tag = f"</dom0:{local}>"
    else:
        open_tag = f"<{tag}>"
        close_tag = f"</{tag}>"
    text = element.text or ""
    return f"{open_tag}{text.strip()}{close_tag}"


def parse_pull_messages(xml_text: str) -> list[dict[str, object]]:
    """Parse a PullMessagesResponse into a list of raw notification dicts.

    Each notification yields a dict with: ``topic`` (the raw topic string, still
    namespace-prefixed -- the adapter canonicalises it), ``utc_time`` (the
    ``UtcTime`` attribute string, or ``None``), ``operation`` (the
    ``PropertyOperation`` -- ``Initialized``/``Changed``/``Deleted`` -- or
    ``None``), ``source`` and ``data`` (name->value maps of the SimpleItems).

    A malformed response yields an empty list rather than raising, so a single
    bad pull never crashes the loop.
    """
    root = parse_xml(xml_text)
    if root is None:
        return []
    notifications: list[dict[str, object]] = []
    for message in root.iter():
        if _local_name(message.tag) != "NotificationMessage":
            continue
        topic = _find_local_text(message, "Topic")
        inner = _find_inner_message(message)
        utc_time: str | None = None
        operation: str | None = None
        source: dict[str, str] = {}
        data: dict[str, str] = {}
        if inner is not None:
            utc_time = inner.get("UtcTime")
            operation = inner.get("PropertyOperation")
            source = _simple_items(inner, "Source")
            data = _simple_items(inner, "Data")
        notifications.append(
            {
                "topic": topic or "",
                "utc_time": utc_time,
                "operation": operation,
                "source": source,
                "data": data,
            }
        )
    return notifications


def _find_inner_message(notification: ET.Element) -> ET.Element | None:
    """Return the inner ``tt:Message`` carrying the event payload.

    A NotificationMessage nests a ``wsnt:Message`` wrapper whose single child is
    the ``tt:Message`` that actually carries the ``UtcTime``/``PropertyOperation``
    attributes and the Source/Data items -- and both elements share the local
    name ``Message``. The payload element is identified as the ``Message`` that
    carries the ``UtcTime`` attribute or has Source/Data children; the last
    ``Message`` in document order (the innermost) is used as the fallback.
    """
    candidates = [
        element
        for element in notification.iter()
        if _local_name(element.tag) == "Message"
    ]
    if not candidates:
        return None
    # The inner tt:Message carries the UtcTime/PropertyOperation attributes; the
    # wsnt:Message wrapper does not. Prefer the attribute-carrying element.
    for element in candidates:
        if element.get("UtcTime") is not None or element.get("PropertyOperation"):
            return element
    # Otherwise prefer one with a *direct* Source/Data child (not a descendant,
    # so the wrapper is not mistaken for the payload), falling back to innermost.
    for element in candidates:
        if any(_local_name(child.tag) in ("Source", "Data") for child in element):
            return element
    return candidates[-1]


def parse_event_topics(xml_text: str) -> list[str]:
    """Parse a GetEventPropertiesResponse into a list of raw topic paths.

    Walks the ``TopicSet`` tree and returns the ``/``-joined path of every leaf
    that is marked as a message topic (``wstop:topic="true"``), with each segment
    being the element's local name. The returned strings are *raw* (unprefixed
    local names joined by ``/``); the adapter feeds them through the shared
    canonicaliser to obtain the stable ``topic_id``. Returns an empty list on a
    malformed response.
    """
    root = parse_xml(xml_text)
    if root is None:
        return []
    topic_set = None
    for element in root.iter():
        if _local_name(element.tag) == "TopicSet":
            topic_set = element
            break
    if topic_set is None:
        return []
    topics: list[str] = []
    _walk_topic_set(topic_set, [], topics)
    return topics


_TOPIC_MARKER = f"{{{NS['wstop']}}}topic"


def _walk_topic_set(element: ET.Element, path: list[str], out: list[str]) -> None:
    """Recursively collect topic paths from a TopicSet subtree.

    An element carrying ``wstop:topic="true"`` is a leaf message topic and its
    accumulated path is emitted. The walk continues into children regardless, so
    a topic that also nests sub-topics still contributes both itself and its
    descendants.
    """
    for child in element:
        local = _local_name(child.tag)
        if not local:
            continue
        child_path = [*path, local]
        marker = child.get(_TOPIC_MARKER) or child.get("topic")
        if marker is not None and marker.strip().lower() == "true":
            out.append("/".join(child_path))
        _walk_topic_set(child, child_path, out)


def _simple_items(message: ET.Element, container_local: str) -> dict[str, str]:
    """Read the ``SimpleItem`` Name/Value pairs under a named container.

    ``container_local`` is the local name of the wrapper (``Source`` or
    ``Data``). Returns a name->value map; a missing container or no items yields
    an empty dict.
    """
    container = _find_local_element(message, container_local)
    if container is None:
        return {}
    items: dict[str, str] = {}
    for element in container.iter():
        if _local_name(element.tag) != "SimpleItem":
            continue
        name = element.get("Name")
        value = element.get("Value")
        if name is not None and value is not None:
            items[name] = value
    return items


def _local_name(tag: object) -> str:
    """Return the local part of a possibly-namespaced ElementTree tag."""
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def _find_local_element(root: ET.Element, local_name: str) -> ET.Element | None:
    """Return the first descendant (or self) whose tag local name matches."""
    if _local_name(root.tag) == local_name:
        return root
    for element in root.iter():
        if _local_name(element.tag) == local_name:
            return element
    return None


def _find_local_text(root: ET.Element, local_name: str) -> str | None:
    """Return the stripped text of the first element with this local name."""
    element = _find_local_element(root, local_name)
    if element is None or element.text is None:
        return None
    text = element.text.strip()
    return text or None
