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
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..db.models import User
from ..db.session import get_session
from ..runtime import get_context
from .token import _extract_bearer, ensure_local_token, verify_token

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
    db: Annotated[Session, Depends(get_session)],
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

    Tests substitute their own principal (or a denying handler) via FastAPI
    dependency overrides keyed on this function.
    """
    settings = get_context().settings
    raw_token = request.cookies.get(settings.session.cookie_name)
    if raw_token is not None:
        return _principal_from_session(db, raw_token, allowed=(_ADMIN_ROLE,))
    return _principal_from_local_token(request)


def require_operator_or_admin_principal(
    request: Request,
    db: Annotated[Session, Depends(get_session)],
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
    raw_token = request.cookies.get(settings.session.cookie_name)
    if raw_token is not None:
        return _principal_from_session(
            db, raw_token, allowed=(_OPERATOR_ROLE, _ADMIN_ROLE)
        )
    return _principal_from_local_token(request)


def _principal_from_session(
    db: Session, raw_token: str, *, allowed: tuple[str, ...]
) -> Principal:
    """Resolve a web session cookie to a principal in ``allowed``, or raise.

    ``401`` if the session is not live; ``403`` if its user's role is not one of
    ``allowed``.
    """
    from .sessions import lookup_session

    settings = get_context().settings.session
    user = lookup_session(db, raw_token, settings=settings)
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

    Mirrors :func:`require_local_token` exactly for the no-cookie path so the
    loopback CLI contract is unchanged: a valid token yields the sentinel
    administrator; a missing or invalid one is ``401``.
    """
    expected = ensure_local_token(get_context().settings)
    received = _extract_bearer(request)
    if received is None or not verify_token(received, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid local API token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Principal(user_id=_SENTINEL_ADMIN_USER_ID, role=_ADMIN_ROLE)
