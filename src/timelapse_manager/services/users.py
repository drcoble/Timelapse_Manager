"""User-administration write use-cases -- the single implementation of the rules.

Each public function performs one write against a caller-supplied session and
returns a :class:`~timelapse_manager.services._base.Result`. The web layer is a
thin adapter over these functions: it parses its form input, calls the service,
and lets its request-scoped session commit on a clean return (rolling back and
re-rendering the form on an :class:`~timelapse_manager.services.errors.AppError`).

Validation and the account-security invariants live here, once. A rule violation
raises a transport-neutral ``AppError`` (``NotFoundError`` for a missing user,
``ConflictError`` for a duplicate username or a lockout-guard refusal,
``ValidationError`` for a weak/short password, a role/confirmation problem, or a
directory account whose credential is not stored here). The web adapter maps each
to the same response the endpoints returned before services existed. Services
never commit and never signal background components; they mutate, ``flush``, and
return -- user writes have no post-commit effect, so every ``Result`` carries an
empty effect tuple.

Password hashing and session revocation are delegated to the existing ``security``
helpers, called from here; this module never reimplements a policy check.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..db.models import Event, User
from ..security import hash_password, revoke_all_user_sessions
from ..usernames import normalize_username
from ._base import Actor, Result
from .errors import ConflictError, NotFoundError, ValidationError

if TYPE_CHECKING:
    import datetime

    from ..config.settings import Settings

# The roles a local account may hold. Kept here (rather than imported from the web
# layer) so the service has no dependency on its callers; the values mirror the
# ``user_role`` column enum.
ADMIN_ROLE = "admin"
OPERATOR_ROLE = "operator"
VIEWER_ROLE = "viewer"
_VALID_ROLES = (ADMIN_ROLE, OPERATOR_ROLE, VIEWER_ROLE)


@dataclass
class UserSpec:
    """Normalised, already-parsed inputs for creating a local account."""

    username: str
    password: str
    role: str
    #: The repeated-password field, when the form supplied one; ``None`` when the
    #: form omitted confirmation entirely (matching the old ``form.get`` default).
    password_confirm: str | None = None


def _now() -> datetime.datetime:
    """Return the current naive-UTC timestamp for an audit row."""
    import datetime

    return datetime.datetime.now(datetime.UTC).replace(tzinfo=None)


def _audit(
    session: Session, actor: Actor, message: str, *, level: str = "info"
) -> None:
    """Append a system-scoped audit event attributed to ``actor``.

    Mirrors the web layer's audit write: the acting user is always a real,
    authenticated administrator whose row already exists, so the ``actor_user_id``
    foreign key is satisfied without materialising the sentinel admin.
    """
    session.add(
        Event(
            scope="system",
            scope_id=None,
            level=level,
            message=message,
            actor_user_id=actor.user_id,
            timestamp=_now(),
        )
    )


def _get_user_or_404(session: Session, user_id: int) -> User:
    """Return a user row or raise :class:`NotFoundError` (mapped to ``404``)."""
    target = session.get(User, user_id)
    if target is None:
        raise NotFoundError("not found")
    return target


def _enabled_admin_count(session: Session) -> int:
    """Count real administrators that can actually sign in (enabled, with a hash).

    This is the lockout basis for *disable*, *delete* and *demote*: an account
    that is disabled or sentinel cannot administer the system, so only enabled
    real admins keep the system reachable.
    """
    return session.execute(
        select(func.count())
        .select_from(User)
        .where(User.role == ADMIN_ROLE)
        .where(User.password_hash.is_not(None))
        .where(User.enabled.is_(True))
    ).scalar_one()


def _is_last_enabled_admin(session: Session, target: User) -> bool:
    """Return True if ``target`` is the only enabled real administrator left.

    Used to refuse a disable, delete or demotion that would leave the system with no
    administrator who can sign in. A non-admin, sentinel, or already-disabled
    target is never the last enabled admin.
    """
    if target.role != ADMIN_ROLE or target.password_hash is None or not target.enabled:
        return False
    return _enabled_admin_count(session) <= 1


def create_user(
    session: Session, actor: Actor, spec: UserSpec, *, settings: Settings
) -> Result[User]:
    """Create a local account and record the create in the audit log.

    Username and password are required; the role must be one of admin/operator/
    viewer; a supplied confirmation must match; and the password must meet the
    configured minimum length. A duplicate username loses the UNIQUE race at flush
    and becomes a ``ConflictError``. Validates, inserts, and flushes -- but does
    not commit; the caller commits.
    """
    # Stored stripped; uniqueness is case-insensitive via ``user.username_key``
    # (#4), so ``Admin`` next to ``admin`` loses at flush -> ConflictError.
    username = normalize_username(spec.username or "")
    password = spec.password
    role = spec.role
    if not username or not password:
        raise ValidationError("username and password are required")
    if role not in _VALID_ROLES:
        raise ValidationError("invalid role", field="role")
    if spec.password_confirm is not None and password != spec.password_confirm:
        raise ValidationError("passwords do not match", field="password")
    if len(password) < settings.auth.password_min_length:
        raise ValidationError("password too short", field="password")

    new_user = User(
        username=username,
        auth_source="local",
        password_hash=hash_password(password, settings.auth),
        role=role,
        enabled=True,
    )
    session.add(new_user)
    try:
        session.flush()
    except IntegrityError as exc:
        # Lost the uniqueness race against a concurrent create (or a duplicate
        # username was submitted): surface it as a conflict rather than a 500.
        session.rollback()
        raise ConflictError(
            f"a user named {username!r} already exists", field="username"
        ) from exc

    _audit(session, actor, f"user {new_user.username!r} ({role}) created")
    return Result(new_user)


def edit_user_role(
    session: Session, actor: Actor, user_id: int, *, role: str
) -> Result[User]:
    """Change a user's role, guarding against demoting the last administrator.

    The role must be admin/operator/viewer. Demoting the last enabled real
    administrator (to operator or viewer) is refused -- which, when self-editing,
    is also the self-demotion-into-lockout case -- so the system keeps an admin
    who can sign in. A disabled admin does not count (S22): it cannot keep the
    system reachable, and demoting one is always allowed.
    The role is only written (and audited) when it actually changes. A missing
    user is a ``404``. Does not commit.
    """
    target = _get_user_or_404(session, user_id)
    if role not in _VALID_ROLES:
        raise ValidationError("Invalid role.", field="role")

    # Any change *away* from admin is a demotion for lockout purposes -- whether
    # to operator or viewer, the account stops counting as a real administrator.
    demoting_admin = target.role == ADMIN_ROLE and role != ADMIN_ROLE
    if demoting_admin and _is_last_enabled_admin(session, target):
        raise ConflictError("Cannot demote the last remaining administrator.")

    if role != target.role:
        target.role = role
        session.flush()
        _audit(session, actor, f"role for user {target.username!r} changed to {role}")
    return Result(target)


def reset_user_password(
    session: Session,
    actor: Actor,
    user_id: int,
    *,
    password: str | None,
    password_confirm: str | None,
    settings: Settings,
) -> Result[User]:
    """Reset a local user's password (revoking their sessions), or just revoke.

    A directory (LDAP) account is refused -- its credential is managed externally
    and is not stored here, so it is never mutated. When a new ``password`` is
    supplied it must be confirmed and meet the configured minimum length, then it
    is hashed and set; in either case the target's sessions are revoked so any
    stolen token dies. A missing user is a ``404``. Does not commit.
    """
    target = _get_user_or_404(session, user_id)
    if target.auth_source != "local":
        raise ValidationError(
            "This account is managed by the directory; its password cannot be set here."
        )

    if password:
        if password_confirm is not None and password != password_confirm:
            raise ValidationError("Passwords do not match.", field="password")
        if len(password) < settings.auth.password_min_length:
            raise ValidationError(
                "Password must be at least "
                f"{settings.auth.password_min_length} characters.",
                field="password",
            )
        target.password_hash = hash_password(password, settings.auth)
        session.flush()
        password_set = True
    else:
        password_set = False

    revoke_all_user_sessions(session, target.id)
    actor_user = session.get(User, actor.user_id)
    actor_name = actor_user.username if actor_user is not None else "system"
    _audit(
        session,
        actor,
        (
            f"password set for user {target.username!r} by {actor_name!r}"
            if password_set
            else f"sessions revoked for user {target.username!r} by {actor_name!r}"
        ),
    )
    return Result(target)


def revoke_user_sessions(session: Session, actor: Actor, user_id: int) -> Result[User]:
    """Revoke all of a user's sessions and record it. A missing user is a ``404``."""
    target = _get_user_or_404(session, user_id)
    revoke_all_user_sessions(session, target.id)
    _audit(session, actor, f"sessions revoked for user {target.username!r}")
    return Result(target)


def disable_user(session: Session, actor: Actor, user_id: int) -> Result[User]:
    """Disable an account so it can no longer sign in, revoking its sessions.

    Refuses to disable the last enabled administrator so the system is never left
    with no one who can sign in. Disabling an already-disabled account is a no-op.
    A missing user is a ``404``. Does not commit.
    """
    target = _get_user_or_404(session, user_id)
    if _is_last_enabled_admin(session, target):
        raise ConflictError("Cannot disable the last remaining administrator.")
    if target.enabled:
        target.enabled = False
        session.flush()
        revoke_all_user_sessions(session, target.id)
        _audit(session, actor, f"user {target.username!r} disabled")
    return Result(target)


def enable_user(session: Session, actor: Actor, user_id: int) -> Result[User]:
    """Re-enable a disabled account. Enabling an enabled account is a no-op.

    A missing user is a ``404``. Does not commit.
    """
    target = _get_user_or_404(session, user_id)
    if not target.enabled:
        target.enabled = True
        session.flush()
        _audit(session, actor, f"user {target.username!r} enabled")
    return Result(target)


def delete_user(session: Session, actor: Actor, user_id: int) -> Result[None]:
    """Remove a user account entirely, revoking its sessions first.

    Refuses to remove the last enabled administrator. The target's sessions are
    revoked before the row is deleted so no live token outlives the account. A
    missing user is a ``404``. Does not commit.
    """
    target = _get_user_or_404(session, user_id)
    if _is_last_enabled_admin(session, target):
        raise ConflictError("Cannot remove the last remaining administrator.")
    username = target.username
    revoke_all_user_sessions(session, target.id)
    session.delete(target)
    session.flush()
    _audit(session, actor, f"user {username!r} removed")
    return Result(None)
