"""Bounded reading and defused parsing of untrusted camera responses.

Every byte a camera sends is untrusted: any device in an allowed subnet, a
compromised camera, or the target of an operator probe controls it. This module
is the one place the camera adapters read and parse those bytes, so three bounds
hold everywhere (#69 CAM-S3, CAM-R12; #67-S24):

* **Size.** :func:`read_capped_response` and :func:`read_capped_body` stream a
  body against a running total and abort with :class:`OtherCaptureError` past a
  cap, so a hostile peer cannot make the process buffer an unbounded response.
* **XML constructs.** :func:`safe_fromstring` parses with :mod:`defusedxml`,
  which refuses any DTD, so entity expansion (billion laughs) and external
  entities never reach the parser. Its input is size-checked first.
* **Depth.** :func:`walk_named_descendants` walks a tree iteratively with a
  depth cap, so deeply nested XML cannot raise :class:`RecursionError` from
  parsers that are documented never to raise.
"""

from __future__ import annotations

import codecs
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any
from xml.etree import ElementTree as ET

import httpx
from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as _defused_fromstring

from .base import OtherCaptureError

logger = logging.getLogger(__name__)

# Ceiling on a SOAP response (ONVIF device/media/event calls, VAPIX
# GetEventInstances). A PullMessages burst of ten notifications, or a full
# event-topic catalogue, is a few tens of KiB; 1 MiB leaves a wide margin.
SOAP_RESPONSE_MAX_BYTES = 1024 * 1024

# Ceiling on the body of a failed response a caller asked to read (a SOAP
# fault). A fault is a few hundred bytes; anything bigger is not one.
ERROR_BODY_MAX_BYTES = 64 * 1024

# Ceiling on a small text response from a VAPIX CGI (param.cgi groups, ptz.cgi
# replies, the Geolocation API document). These are a few KiB at most.
TEXT_RESPONSE_MAX_BYTES = 256 * 1024

# Default ceiling on an XML document handed to :func:`safe_fromstring`. It
# matches the SOAP cap, the largest XML response the adapters read.
XML_MAX_BYTES = SOAP_RESPONSE_MAX_BYTES

# Deepest element path :func:`walk_named_descendants` follows. Real ONVIF and
# VAPIX topic trees are under 20 levels deep.
XML_MAX_DEPTH = 64


@dataclass(frozen=True)
class CappedResponse:
    """The parts of a camera HTTP response the adapters act on.

    ``text`` is the decoded body for a ``2xx`` response and ``""`` otherwise:
    callers turn any other status into an error without reading the body, so it
    is never downloaded -- unless the caller asked for it with
    ``read_error_body`` (see :func:`read_capped_response`).
    """

    status_code: int
    headers: httpx.Headers
    text: str


async def read_capped_body(
    response: httpx.Response, *, max_bytes: int, label: str, url: str
) -> bytes:
    """Stream a response body, aborting once it would exceed ``max_bytes``.

    A declared ``Content-Length`` over the cap is rejected before any byte is
    read. A stream that overshoots mid-read, or sends no length at all, is caught
    by the running total. Chunks accumulate into one ``bytearray``, so the peak
    is the body itself, not the body plus a joined copy. Leaving the caller's
    ``client.stream`` block on the raise releases the connection, so a hostile
    peer cannot pin it open.

    :param label: what the body is ("snapshot", "response"), for the message.
    :raises OtherCaptureError: when the body is, or declares itself, over the cap.
    """
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
        except ValueError:
            declared_bytes = None  # Unparseable: the running total still applies.
        if declared_bytes is not None and declared_bytes > max_bytes:
            raise OtherCaptureError(
                f"{label} from {url} declares {declared_bytes} bytes, "
                f"over the {max_bytes}-byte limit"
            )
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > max_bytes:
            raise OtherCaptureError(
                f"{label} from {url} exceeded the {max_bytes}-byte limit"
            )
        body += chunk
    return bytes(body)


async def read_capped_response(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    max_bytes: int,
    read_error_body: bool = False,
    **kwargs: Any,
) -> CappedResponse:
    """Send one request and read a ``2xx`` body of at most ``max_bytes``.

    The request is streamed (``client.stream``) rather than buffered, so the cap
    applies while the body arrives. ``kwargs`` pass through to
    ``client.stream`` (``content``, ``headers``, ``timeout``, ``auth``). httpx
    transport and timeout errors propagate unchanged, so each caller keeps its
    own mapping to :class:`CaptureError`.

    The body is decoded with the response's declared charset, falling back to
    UTF-8 for a missing or unknown one, and undecodable bytes are replaced: the
    parsers downstream treat malformed content as "no result".

    :param read_error_body: also read a non-``2xx`` body, capped at
        :data:`ERROR_BODY_MAX_BYTES` (a SOAP fault names why the call failed,
        #69 CAM-R9); one over the cap reads as empty. Off by default: the
        body of a failed response is not downloaded.
    :raises OtherCaptureError: when a ``2xx`` body exceeds ``max_bytes``.
    """
    async with client.stream(method, url, **kwargs) as response:
        if not 200 <= response.status_code < 300:
            text = ""
            if read_error_body:
                try:
                    error_body = await read_capped_body(
                        response,
                        max_bytes=min(max_bytes, ERROR_BODY_MAX_BYTES),
                        label="error response",
                        url=url,
                    )
                except OtherCaptureError:
                    # An oversized error body: the status alone still decides.
                    error_body = b""
                text = error_body.decode(_charset(response), errors="replace")
            return CappedResponse(response.status_code, response.headers, text)
        body = await read_capped_body(
            response, max_bytes=max_bytes, label="response", url=url
        )
        return CappedResponse(
            response.status_code,
            response.headers,
            body.decode(_charset(response), errors="replace"),
        )


def _charset(response: httpx.Response) -> str:
    """Return the response's declared charset if Python knows it, else UTF-8."""
    declared = response.charset_encoding
    if declared:
        try:
            return codecs.lookup(declared).name
        except LookupError:
            pass
    return "utf-8"


def safe_fromstring(
    data: str | bytes, *, max_bytes: int = XML_MAX_BYTES
) -> ET.Element | None:
    """Parse untrusted XML, returning the root element or ``None``.

    ``None`` covers every way the document can be unacceptable, matching the
    "malformed means no result" contract of the parsers that call this:

    * it is larger than ``max_bytes`` (UTF-8 encoded size for ``str`` input);
    * it is not well-formed XML;
    * it carries a DTD. defusedxml refuses every DOCTYPE (``forbid_dtd``), which
      also rules out entity declarations, so neither entity expansion nor an
      external entity is ever processed. ONVIF, WS-Discovery and VAPIX never use
      a DTD, so no legitimate camera response is refused.

    The returned tree is made of standard ``xml.etree`` elements, whose
    ``iter()`` and ``find()`` are iterative and safe on deep nesting.
    """
    if _encoded_size_exceeds(data, max_bytes):
        logger.debug("camera XML over the %d-byte limit; ignored", max_bytes)
        return None
    try:
        return _defused_fromstring(data, forbid_dtd=True)
    except DefusedXmlException as exc:
        # A camera never sends a DTD; this is a broken or hostile device.
        logger.warning("camera XML refused: %s", type(exc).__name__)
        return None
    except ET.ParseError:
        return None


def _encoded_size_exceeds(data: str | bytes, max_bytes: int) -> bool:
    """Return whether ``data`` is over ``max_bytes`` once UTF-8 encoded.

    A ``str`` of more than ``max_bytes`` characters is over without encoding it
    (every character takes at least one byte); otherwise its UTF-8 length is
    measured, which is at most ``4 * max_bytes`` of work.
    """
    if len(data) > max_bytes:
        return True
    if isinstance(data, bytes):
        return False
    return len(data.encode("utf-8", errors="surrogatepass")) > max_bytes


def local_name(tag: object) -> str:
    """Return the local part of a possibly-namespaced ElementTree tag.

    Comments and processing instructions have a non-string tag and yield ``""``.
    """
    if not isinstance(tag, str):
        return ""
    return tag.rsplit("}", 1)[-1]


def walk_named_descendants(
    root: ET.Element,
    *,
    skip: frozenset[str] = frozenset(),
    max_depth: int = XML_MAX_DEPTH,
) -> Iterator[tuple[ET.Element, tuple[str, ...]]]:
    """Yield every named descendant of ``root`` with its path of local names.

    The walk is depth-first and in document order (pre-order), the order the
    recursive walkers it replaces produced, but it uses an explicit stack, so
    nesting depth costs memory, never Python recursion. ``root`` itself is not
    yielded; the path of one of its children is ``(child_local_name,)``.

    An element is not yielded, and its subtree is not entered, when its local
    name is empty (a comment or processing instruction) or is in ``skip``, or
    when its path would be longer than ``max_depth``. Anything below the depth
    cap is ignored, not an error.
    """
    stack: list[tuple[ET.Element, tuple[str, ...]]] = [
        (child, ()) for child in reversed(root)
    ]
    truncated = False
    while stack:
        element, parent_path = stack.pop()
        local = local_name(element.tag)
        if not local or local in skip:
            continue
        if len(parent_path) >= max_depth:
            truncated = True
            continue
        path = (*parent_path, local)
        yield element, path
        stack.extend((child, path) for child in reversed(element))
    if truncated:
        logger.debug("camera XML deeper than %d levels; the rest ignored", max_depth)
