"""One rule for "is this a safe same-site path to send the browser to?" (#67 S21).

A redirect target that comes from a request -- the login ``next`` field, the
problem report's ``page`` field, the ``HX-Current-URL`` header an HTMX request
carries -- is attacker-influenceable. :func:`safe_local_path` is the single check
every such value goes through before it is used as a ``Location``, an
``HX-Redirect`` or an ``href``: it returns the value when it is an absolute path
on this origin and ``None`` otherwise.

Browsers do more to a URL than the server sees. They strip tab, CR and LF
anywhere in it, turn ``\\`` into ``/``, and resolve ``.`` and ``..`` segments, so
``/\\t/evil.example`` becomes ``//evil.example`` and ``/./partials/x`` becomes
``/partials/x``. Everything a browser would rewrite is refused up front, and the
path is checked a second time after one round of percent-decoding, so an encoded
form cannot slip past a later component that decodes it.
"""

from __future__ import annotations

import urllib.parse

#: Longest value accepted; a redirect target has no use for more.
MAX_PATH_LENGTH = 2048

# Server-rendered fragment/partial endpoints (HTMX poll targets, panel refreshes)
# are not navigable pages: landing on one shows an unstyled partial. A target
# pointing at one -- whether stale or crafted -- is refused. These are GET
# fragment routes only; the matching POST actions never reach a redirect.
NON_NAVIGABLE_PREFIXES = ("/partials/", "/alerts/")

_DOT_SEGMENTS = frozenset({".", ".."})


def _plain_ascii(value: str) -> bool:
    """Return whether ``value`` is printable ASCII with no backslash.

    Below ``0x21`` covers space and every control character (a browser deletes
    tab, CR and LF inside a URL); ``0x7F`` is DEL; above ``0x7E`` is non-ASCII.
    """
    return all(0x20 < ord(c) < 0x7F and c != "\\" for c in value)


def safe_local_path(
    value: str | None, *, max_length: int = MAX_PATH_LENGTH
) -> str | None:
    """Return ``value`` if it is a safe absolute path on this origin, else ``None``.

    Accepted: a path starting with exactly one ``/`` -- optionally followed by a
    query and a fragment -- that is printable ASCII, contains no backslash, no
    ``.`` or ``..`` segment and is not a fragment endpoint
    (:data:`NON_NAVIGABLE_PREFIXES`).
    The path is also checked after one round of percent-decoding: an encoded
    slash, backslash, control character or dot segment is refused too. The query
    is left encoded and unexamined beyond the character rule, because it cannot
    change where the browser goes.

    Refused, among others: ``//host``, ``/\\host``, ``\\\\host``, ``https://host``,
    ``javascript:...``, a relative path, ``/%2F%2Fhost`` and ``/%5Chost``.
    """
    if not value or len(value) > max_length or not _plain_ascii(value):
        return None
    if not value.startswith("/") or value.startswith("//"):
        return None
    parts = urllib.parse.urlsplit(value)
    if parts.scheme or parts.netloc:
        return None
    path = parts.path
    decoded = urllib.parse.unquote(path)
    if decoded != path:
        # One round only: a second would make ``%252F`` a slash that no
        # component downstream will ever see, and so refuse harmless input.
        if not decoded.startswith("/") or decoded.startswith("//"):
            return None
        if any(ord(c) < 0x20 or ord(c) == 0x7F or c == "\\" for c in decoded):
            return None
    if any(segment in _DOT_SEGMENTS for segment in decoded.split("/")):
        return None
    if decoded.startswith(NON_NAVIGABLE_PREFIXES):
        return None
    return value
