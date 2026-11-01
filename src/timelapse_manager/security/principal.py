"""Identity and authorization seam for privileged mutations.

The local bearer token (see :mod:`.token`) answers *whether* a caller may reach
the API at all. This module answers the separate question of *who* the caller is
and *what role* they hold, so write operations can be attributed to an actor and
gated on privilege.

Role storage and lookup are introduced in a later phase. Until then a single
sentinel administrator is returned, but every privileged endpoint already depends
on :func:`require_admin_principal` rather than constructing a principal inline.
That makes this function the one chokepoint a real lookup slots into without
touching any call site, and the single place tests override to exercise the
denied-access path.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from sqlalchemy.orm import Session

from ..db.models import User
from ..db.session import DbSessionDep
from ..runtime import get_context
from .authz import session_cookie_value
from .token import (
    _extract_bearer,
    current_local_token,
    current_metrics_token,
    enforce_token_peer,
    invalid_token_error,
    peer_allowed,
    refuse_token_peer,
    verify_token,
)

# The sentinel administrator returned until real identities exist. Its id is used
# as the actor on every audit event a privileged mutation writes, so audit rows
# carry a stable, resolvable attribution from the first release.
_SENTINEL_ADMIN_USER_ID = 1
_SENTINEL_ADMIN_USERNAME = "system"
_ADMIN_ROLE = "admin"
_OPERATOR_ROLE = "operator"


def ensure_sentinel_admin(session: Session) -> int:
    """Ensure the sentinel administrator row exists and return its id.

    Audit events carry a foreign key to ``user.id``, so the actor recorded for a
    privileged mutation must be a real row. Until real account management seeds
    administrators, this lazily materialises a single ``"system"`` user with a
    fixed id the first time any mutation is audited. Idempotent: subsequent calls
    find the existing row. Caller is responsible for committing the surrounding
    transaction.

    A later phase that seeds real administrators removes this helper and its one
    call site; explicit identities then supply the actor id instead.
    """
    if session.get(User, _SENTINEL_ADMIN_USER_ID) is None:
        session.add(
            User(
                id=_SENTINEL_ADMIN_USER_ID,
                username=_SENTINEL_ADMIN_USERNAME,
                auth_source="local",
                role=_ADMIN_ROLE,
            )
        )
        session.flush()
    return _SENTINEL_ADMIN_USER_ID


@dataclass(frozen=True)
class Principal:
    """The authenticated caller behind a privileged request.

    :param user_id: the acting user's id, recorded as the actor on audit events.
    :param role: the caller's role; ``"admin"`` is required for mutations today.
    """

    user_id: int
    role: str


def require_admin_principal(
    request: Request,
    db: DbSessionDep,
) -> Principal:
    """FastAPI dependency yielding the administrator behind a mutation.

    Resolves the caller along one of two paths and rejects everything else:

    * **Web (cookie) caller.** If the request carries the session cookie, it is
      resolved through the session/role layer: an invalid or expired session is
      ``401``; a valid session whose user is not an administrator is ``403``; a
      valid admin session yields a principal for that real user.
    * **CLI (bearer-token) caller.** A request with no session cookie falls back
      to the local bearer token. A valid token yields the sentinel administrator
      principal exactly as before, preserving every existing loopback CLI caller;
      a missing or invalid token is ``401``.

    Under ``/api/v1`` the router's bearer dependency has already required a valid
    bearer, so there the cookie path only applies to a request that carries both
    (#261). Tests substitute their own principal (or a denying handler) via
    FastAPI dependency overrides keyed on this function.
    """
    settings = get_context().settings
    raw_token = session_cookie_value(request, settings.session)
    if raw_token is not None:
        return _principal_from_session(request, db, allowed=(_ADMIN_ROLE,))
    return _principal_from_local_token(request)


def require_operator_or_admin_principal(
    request: Request,
    db: DbSessionDep,
) -> Principal:
    """FastAPI dependency yielding an operator-or-admin behind a mutation.

    Gates the day-to-day operational surface (cameras, projects, renders,
    frames). Resolution mirrors :func:`require_admin_principal` along the same
    two paths, widening only the accepted role:

    * **Web (cookie) caller.** A valid session whose user is an operator *or*
      admin yields a principal for that real user; any other role is ``403`` and
      an invalid/expired session is ``401``.
    * **CLI (bearer-token) caller.** A request with no session cookie falls back
      to the local bearer token and yields the sentinel administrator, exactly as
      the admin gate does, so every loopback CLI caller is preserved.

    Tests substitute their own principal (or a denying handler) via FastAPI
    dependency overrides keyed on this function.
    """
    settings = get_context().settings
    raw_token = session_cookie_value(request, settings.session)
    if raw_token is not None:
        return _principal_from_session(
            request, db, allowed=(_OPERATOR_ROLE, _ADMIN_ROLE)
        )
    return _principal_from_local_token(request)


def _principal_from_session(
    request: Request, db: Session, *, allowed: tuple[str, ...]
) -> Principal:
    """Resolve the request's session cookie to a principal in ``allowed``, or raise.

    ``401`` if the session is not live; ``403`` if its user's role is not one of
    ``allowed``. The session is resolved at most once per request (#67 R3), and a
    passive poll still does not touch it (the same rule as every other layer).
    """
    from .sessions import live_principal_from_cache, resolve_request_session

    settings = get_context().settings.session
    resolved = resolve_request_session(request, db, settings=settings)
    # The cached answer may predate a slow body read: re-check access live and
    # take the role from the live user row (#207).
    user = live_principal_from_cache(db, resolved) if resolved is not None else None
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required.",
        )
    if user.role not in allowed:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Insufficient privileges.",
        )
    return Principal(user_id=user.id, role=user.role)


def _principal_from_local_token(request: Request) -> Principal:
    """Validate the CLI bearer token and yield the sentinel admin principal.

    Mirrors :func:`require_local_token` exactly for the no-cookie path, so the
    routes outside the bearer router (metrics, debug) apply the same peer
    policy: a refused peer is ``403`` before any compare, an unusable token file
    is ``503``, a missing or invalid token is ``401``, and a valid one yields
    the sentinel administrator.
    """
    enforce_token_peer(request, get_context().settings.server.api_token_allowed_clients)
    expected = current_local_token()
    received = _extract_bearer(request)
    if received is None or not verify_token(received, expected):
        raise invalid_token_error()
    return Principal(user_id=_SENTINEL_ADMIN_USER_ID, role=_ADMIN_ROLE)


def require_metrics_reader(request: Request, db: DbSessionDep) -> Principal | None:
    """FastAPI dependency gating the metrics scrape (#67 S2).

    Accepts, in order:

    * an **administrator session cookie** (unchanged; a non-admin is ``403``);
    * the **local admin token**, under the local-token peer policy (loopback,
      or HTTPS from ``server.api_token_allowed_clients``); yields the sentinel
      administrator;
    * the **metrics token**, from a peer in
      ``observability.metrics_allowed_clients``, over HTTP or HTTPS (it only
      reads); yields ``None``, since it is not a user.

    A peer allowed for neither token is ``403`` before any compare. A missing or
    wrong token from an allowed peer is ``401``. The metrics token is accepted
    nowhere else: every other route compares against the local token only.
    """
    settings = get_context().settings
    raw_cookie = session_cookie_value(request, settings.session)
    if raw_cookie is not None:
        return _principal_from_session(request, db, allowed=(_ADMIN_ROLE,))

    trusted = settings.server.trusted_proxies
    admin_peer = peer_allowed(
        request,
        settings.server.api_token_allowed_clients,
        trusted_proxies=trusted,
        require_https=True,
    )
    metrics_peer = peer_allowed(
        request,
        settings.observability.metrics_allowed_clients,
        trusted_proxies=trusted,
        require_https=False,
    )
    if not (admin_peer or metrics_peer):
        raise refuse_token_peer(
            request,
            "metrics/API token",
            "observability.metrics_allowed_clients",
        )

    received = _extract_bearer(request)
    if received is None:
        raise invalid_token_error()
    if admin_peer and verify_token(received, current_local_token()):
        return Principal(user_id=_SENTINEL_ADMIN_USER_ID, role=_ADMIN_ROLE)
    if metrics_peer and verify_token(received, current_metrics_token()):
        return None
    raise invalid_token_error()
