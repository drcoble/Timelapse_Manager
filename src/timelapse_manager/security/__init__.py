"""Security primitives: authentication, sessions, CSRF, and secret storage.

This package is the single import surface for the web auth/security layer. The
notable public pieces and the contract callers rely on:

Passwords (:mod:`.passwords`)
    ``hash_password``, ``verify_password``, ``needs_rehash`` -- Argon2id, with
    cost read from ``Settings.auth``. ``verify_password`` is constant-time and
    treats a missing hash as a non-match.

Sessions (:mod:`.sessions`)
    ``create_session``, ``lookup_session``, ``rotate_session``,
    ``revoke_session``, ``revoke_all_user_sessions``, plus ``get_session_row``
    for callers needing the row (e.g. its per-session CSRF secret) and
    ``hash_token``. The raw token is returned only at creation/rotation; only its
    SHA-256 hash is stored. Cookie/timeout policy comes from
    ``Settings.session`` -- the cookie is named ``session.cookie_name`` and the
    caller setting it must use ``HttpOnly``, ``SameSite`` per
    ``session.samesite``, and ``Secure`` when the effective scheme is HTTPS.

CSRF (:mod:`.csrf`)
    ``issue_csrf`` / ``verify_csrf`` -- per-session synchronizer token derived
    from the session's CSRF secret; ``verify_csrf`` is constant-time. The CSRF
    rule keys on **session-cookie presence** (see ``has_session_cookie``), not on
    an ``Authorization`` header: cookie-authenticated requests require CSRF,
    bearer-token (CLI) requests are exempt.

Authorization (:mod:`.authz`)
    ``require_authenticated_session`` (``401`` if no live cookie session),
    ``require_role`` (deny-by-default; ``403`` otherwise),
    ``require_operator_or_admin`` (the operator-or-admin web gate built on
    ``require_role``), and ``has_session_cookie`` (the CSRF discriminator).

Login / bootstrap (:mod:`.login`)
    ``authenticate_user`` (local accounts only; generic ``None`` on any
    failure), ``first_run_needed``, ``create_initial_admin``, ``change_password``
    (revokes all of the user's sessions).

Throttling (:mod:`.throttle`)
    ``BruteForceThrottle`` -- per-IP primary limit plus a non-enumerating
    per-username component.

Encryption at rest (:mod:`.crypto`, :mod:`.keystore`)
    ``encrypt_secret`` / ``decrypt_secret`` -- Fernet encryption of stored
    credentials with a versioned ``enc:v1:`` prefix; a value lacking the prefix
    is treated as legacy plaintext and returned unchanged. ``encrypt_credentials``
    / ``decrypt_credentials`` apply the same to a camera credential document.
    ``set_key_provider`` installs the key source; ``build_key_provider`` selects
    the OS keystore (Keychain / Credential Manager / Secret Service) when reachable
    and otherwise a restricted-permission (``0600``) key file, refusing a group- or
    world-readable file. The key is never logged or committed.

Legacy/CLI surface preserved unchanged: ``require_local_token`` (CLI bearer),
``require_admin_principal`` (dual-path: cookie session for web, bearer token for
CLI), ``require_operator_or_admin_principal`` (the same dual-path gate widened to
admit operators, used by the operational mutation routes), and
``ensure_sentinel_admin``.
"""

from __future__ import annotations

from .authz import (
    has_session_cookie,
    require_authenticated_session,
    require_operator_or_admin,
    require_role,
)
from .crypto import (
    decrypt_credentials,
    decrypt_secret,
    encrypt_credentials,
    encrypt_secret,
    set_key_provider,
)
from .csrf import issue_csrf, verify_csrf
from .keystore import KeyProvider, build_key_provider
from .login import (
    authenticate_user,
    change_password,
    create_initial_admin,
    create_local_user,
    first_run_needed,
)
from .passwords import hash_password, needs_rehash, verify_password
from .principal import (
    Principal,
    ensure_sentinel_admin,
    require_admin_principal,
    require_operator_or_admin_principal,
)
from .sessions import (
    create_session,
    get_session_row,
    hash_token,
    lookup_session,
    revoke_all_user_sessions,
    revoke_session,
    rotate_session,
)
from .throttle import BruteForceThrottle
from .token import ensure_local_token, require_local_token, verify_token

__all__ = [
    "BruteForceThrottle",
    "KeyProvider",
    "Principal",
    "authenticate_user",
    "build_key_provider",
    "change_password",
    "create_initial_admin",
    "create_local_user",
    "create_session",
    "decrypt_credentials",
    "decrypt_secret",
    "encrypt_credentials",
    "encrypt_secret",
    "ensure_local_token",
    "ensure_sentinel_admin",
    "first_run_needed",
    "get_session_row",
    "has_session_cookie",
    "hash_password",
    "hash_token",
    "issue_csrf",
    "lookup_session",
    "needs_rehash",
    "require_admin_principal",
    "require_authenticated_session",
    "require_local_token",
    "require_operator_or_admin",
    "require_operator_or_admin_principal",
    "require_role",
    "revoke_all_user_sessions",
    "revoke_session",
    "rotate_session",
    "set_key_provider",
    "verify_csrf",
    "verify_password",
    "verify_token",
]
