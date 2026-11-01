"""Per-session CSRF synchronizer tokens, masked per response.

This implements the synchronizer-token pattern: each server-side session carries
a high-entropy ``csrf_secret`` (minted in :mod:`.sessions`). The CSRF token
embedded in a page's forms is derived from that secret, and a state-changing
request must echo a matching token back. Because the secret is bound to the
session and never leaves it except as an issued token, a cross-site request --
which cannot read the page to learn the token -- cannot forge a match. The
secret rotates whenever the session rotates (every login mints a new row).

**Masking (#70 F-S4).** An issued token is never the secret itself. Each call
to :func:`issue_csrf` draws a fresh one-time pad the length of the secret and
returns ``base64url(pad + (pad XOR secret))``. Every response therefore carries
a different token of the same length, so a compression side channel (BREACH:
a proxy that compresses HTML which also reflects query parameters) has no stable
secret to extract byte by byte. :func:`verify_csrf` unmasks a presented token
and compares the result with the secret. It accepts only a masked token: the bare
secret is refused (the compatibility path for pre-masking tabs was retired after
0.2.0, #188).

**Fetch metadata.** :func:`fetch_site_rejected` refuses an unsafe request the
browser itself labels ``cross-site`` or ``same-site`` (a different origin is
rejected), even when it carries a valid token.

Comparison is constant-time to avoid leaking a partial match through timing.
CSRF tokens are not secrets in the credential sense but are still never logged.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
from collections.abc import Mapping

from ..db.models import Session as SessionRow

# A masked token is unpadded base64url; anything else is not one.
_MASKED_TOKEN = re.compile(r"[A-Za-z0-9_-]+")

# ``Sec-Fetch-Site`` values that mean "not this origin". ``same-origin``,
# ``none`` (a user-initiated navigation) and an absent header (an older browser,
# the CLI) fall through to the token check.
_REJECTED_FETCH_SITES = frozenset({"cross-site", "same-site"})


def _secret_bytes(secret: str) -> bytes:
    """Encode a secret (or a presented value) for byte-wise comparison.

    ``surrogatepass`` so a lone surrogate in a presented value is just a
    mismatch, never a ``UnicodeEncodeError`` (#115).
    """
    return secret.encode("utf-8", "surrogatepass")


def _xor(left: bytes, right: bytes) -> bytes:
    """XOR two equal-length byte strings."""
    return bytes(a ^ b for a, b in zip(left, right, strict=True))


def _masked_length(secret_length: int) -> int:
    """Return the unpadded base64 length of a token masking ``secret_length`` bytes."""
    return (4 * 2 * secret_length + 2) // 3


def mask_secret(secret: str) -> str:
    """Return a freshly masked token for ``secret``.

    The pad comes from :func:`secrets.token_bytes`, so two calls never return the
    same token, and the token reveals nothing about the secret without the pad
    it carries.
    """
    raw = _secret_bytes(secret)
    pad = secrets.token_bytes(len(raw))
    blob = pad + _xor(pad, raw)
    return base64.urlsafe_b64encode(blob).rstrip(b"=").decode("ascii")


def _unmask(presented: str, secret_length: int) -> bytes | None:
    """Return the secret a masked token carries, or ``None`` if it is not one.

    Strict on purpose: exactly the masked length, only the base64url alphabet,
    no padding, and exactly ``2 * secret_length`` decoded bytes. Anything else
    is "not a masked token" and is refused.
    """
    if len(presented) != _masked_length(secret_length):
        return None
    if _MASKED_TOKEN.fullmatch(presented) is None:
        return None
    padded = presented + "=" * (-len(presented) % 4)
    try:
        blob = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return None
    if len(blob) != 2 * secret_length:
        return None
    return _xor(blob[:secret_length], blob[secret_length:])


def issue_csrf(session_or_secret: SessionRow | str) -> str:
    """Return a masked CSRF token to embed in a response for a session.

    Accepts either the session row (from which the per-session ``csrf_secret``
    is read) or the secret string directly, so callers holding only one or the
    other can both issue without an extra lookup. Each call returns a different
    token; every one of them verifies against the same secret.

    :raises ValueError: if a session row is passed that has no CSRF secret (a
        session created outside the web login flow); such a session cannot
        participate in CSRF-protected form submission.
    """
    if isinstance(session_or_secret, str):
        return mask_secret(session_or_secret)
    secret = session_or_secret.csrf_secret
    if not secret:
        raise ValueError("Session has no CSRF secret to issue a token from.")
    return mask_secret(secret)


def verify_csrf(expected: str | None, presented: str | None) -> bool:
    """Return True if ``presented`` is a masked token for the secret ``expected``.

    ``presented`` must be what :func:`issue_csrf` returns. The bare secret is not
    accepted (#188): a value that does not unmask is a mismatch. The comparison is
    :func:`secrets.compare_digest` over bytes, so a partial match is not
    distinguishable by timing and a non-ASCII value is a mismatch rather than a
    ``TypeError`` (#115). A missing expected secret (session without CSRF state)
    or a missing presented token never matches, and a malformed value never
    raises.
    """
    if not expected or not presented:
        return False
    expected_bytes = _secret_bytes(expected)
    candidate = _unmask(presented, len(expected_bytes))
    if candidate is None:
        return False
    return secrets.compare_digest(expected_bytes, candidate)


def fetch_site_rejected(headers: Mapping[str, str]) -> bool:
    """Return True if the browser marked this request as from another site.

    Reads ``Sec-Fetch-Site`` (a forbidden header name: page script cannot set
    it). ``cross-site`` and ``same-site`` are rejected: the app is served from
    one origin, and a request from any other origin is refused.
    ``same-origin``, ``none`` and a missing header return False, leaving the
    decision to the token. ``Origin`` is deliberately not used: under
    ``Referrer-Policy: no-referrer`` a same-origin form sends ``Origin: null``.

    ``headers`` must look the name up case-insensitively (Starlette's
    ``Headers`` does) or be keyed in lower case.
    """
    value = headers.get("sec-fetch-site")
    if not value:
        return False
    return value.strip().lower() in _REJECTED_FETCH_SITES
