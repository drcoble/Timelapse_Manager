"""Per-session CSRF synchronizer tokens.

This implements the synchronizer-token pattern: each server-side session carries
a high-entropy ``csrf_secret`` (minted in :mod:`.sessions`). The CSRF token
embedded in a page's forms is derived from that secret, and a state-changing
request must echo a matching token back. Because the secret is bound to the
session and never leaves it except as the issued token, a cross-site request --
which cannot read the page to learn the token -- cannot forge a match.

The token here is the session secret itself: it is already high-entropy,
unguessable, per-session, and rotates whenever the session rotates (login),
so a separate derivation buys nothing. Comparison is constant-time to avoid
leaking a partial match through timing. CSRF tokens are not secrets in the
credential sense but are still never logged.
"""

from __future__ import annotations

import secrets

from ..db.models import Session as SessionRow


def issue_csrf(session_or_secret: SessionRow | str) -> str:
    """Return the CSRF token to embed in forms for a session.

    Accepts either the session row (from which the per-session ``csrf_secret``
    is read) or the secret string directly, so callers holding only one or the
    other can both issue without an extra lookup.

    :raises ValueError: if a session row is passed that has no CSRF secret (a
        session created outside the web login flow); such a session cannot
        participate in CSRF-protected form submission.
    """
    if isinstance(session_or_secret, str):
        return session_or_secret
    secret = session_or_secret.csrf_secret
    if not secret:
        raise ValueError("Session has no CSRF secret to issue a token from.")
    return secret


def verify_csrf(expected: str | None, presented: str | None) -> bool:
    """Return True if ``presented`` matches the session's ``expected`` token.

    Constant-time comparison via :func:`secrets.compare_digest` so a partial
    match is not distinguishable by timing. A missing expected secret (session
    without CSRF state) or a missing presented token never matches.
    """
    if not expected or not presented:
        return False
    return secrets.compare_digest(expected, presented)
